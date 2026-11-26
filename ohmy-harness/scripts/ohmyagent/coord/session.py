"""主控协调层。

所有状态变更都走 `Coordinator.execute(cmd)`：

1. 在锁内读取当前记录（或已有的内存视图）；
2. 在内存中校验并产生新记录；
3. 追加事件并 `fsync`，失败则回退文件内容；
4. 成功后用回放结果替换内存视图。

锁在进程退出后自动释放（文件锁），因此主控崩溃后重启不需要手工清理。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from math import isfinite
from pathlib import Path
from typing import Any, Iterator

from ..core.errors import ConflictError
from ..core.lifecycle import TaskStatus
from ..store import commands as C
from ..store.ledger import CorruptLedgerError, Ledger, stable_hash
from ..store.record import Event, Kind, TaskRecord, TaskSnapshot
from ..util.clock import now_utc
from .startup import register_review_hook, require_review_hook

try:  # pragma: no cover - 平台差异
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None
    import msvcrt as _msvcrt


def _rehydrate(cmd: Any) -> Any:
    """命令从别的进程（pickle）带过来时，恢复默认的 Limits 对象。

    跨进程调用不能假设默认值仍是 ``Limits`` 实例，否则上限校验会静默失效。
    """
    from ..core.lifecycle import DEFAULT_LIMITS, Limits

    if not hasattr(cmd, "limits"):
        return cmd
    limits = getattr(cmd, "limits")
    if isinstance(limits, Limits):
        return cmd
    try:
        from dataclasses import replace

        return replace(cmd, limits=DEFAULT_LIMITS)  # type: ignore[type-var]
    except TypeError:
        return cmd


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        if _fcntl is not None:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        else:  # Windows：阻塞式独占锁，进程退出后系统自动释放
            fh.seek(0)
            _msvcrt.locking(fh.fileno(), _msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)


class CorruptionError(CorruptLedgerError):
    code = "corrupt_ledger"


class Coordinator:
    """持有任务记录的内存视图，负责所有写操作。"""

    def __init__(self, state: Path) -> None:
        self.state = Path(state)
        self.ledger = Ledger(self.state)
        self._records: dict[str, TaskRecord] | None = None

    # ---- 读取 -----------------------------------------------------------------

    def _load(self) -> dict[str, TaskRecord]:
        if self._records is None:
            self._records = replay(self.ledger.read())
        return self._records

    def invalidate(self) -> None:
        self._records = None

    def record(self, task_id: str) -> TaskRecord | None:
        return self._load().get(task_id)

    def snapshot(self, task_id: str) -> TaskSnapshot | None:
        record = self.record(task_id)
        return TaskSnapshot.of(record) if record else None

    def records(self) -> dict[str, TaskRecord]:
        return self._load()

    def initialize(self) -> dict[str, str]:
        with file_lock(self.ledger.lock):
            return register_review_hook(self.state)

    # ---- 写 -------------------------------------------------------------------

    def execute(self, cmd: Any) -> Event:
        cmd = _rehydrate(cmd)
        if isinstance(cmd, C.DispatchTask) and cmd.use_worker is not None:
            return self._run_worker(cmd)
        with file_lock(self.ledger.lock):
            return self._execute_locked(cmd)

    def _run_worker(self, cmd: C.DispatchTask) -> Event:
        from ..workers import ExecWorker, WorkerError, WorkSpec, reject_legacy_self

        worker = cmd.use_worker
        name = getattr(worker, "name", "worker")
        limits = [value for value in (cmd.timeout, cmd.limits.max_timeout) if value is not None]
        if any(not isfinite(value) or value <= 0 for value in limits):
            raise ConflictError("Worker 硬时限须为正的有限秒数")
        timeout = min(limits, default=None)
        # 启动记录先落盘。长考期间其他主控操作仍可核对、记事或取消。
        with file_lock(self.ledger.lock):
            launch = replace(cmd, use_worker=None, session=cmd.session or f"{name}:{cmd.task_id}:1", model=cmd.model or name)
            started = self._execute_locked(launch)
            record = self._load()[cmd.task_id]
            spec = WorkSpec(record.id, record.intent, started.data["workspace"], tuple(record.inputs), tuple(record.outputs),
                            tuple(record.write_scope), dict(record.constraints), cmd.model, cmd.effort)

        number = started.data["attempt"]

        def remember_session(session: str) -> None:
            self.execute(C.RecordEvent(cmd.task_id, "worker.session", {"attempt": number, "session": session}))

        try:
            reject_legacy_self(name)
            if isinstance(worker, ExecWorker):
                spec = replace(spec, on_session=remember_session)
            result = worker.run(spec, timeout=timeout)
        except WorkerError as exc:
            returned = C.WorkerReturned(cmd.task_id, number, exc.session, kind=exc.kind, reason=str(exc), worker=name)
        else:
            returned = C.WorkerReturned(cmd.task_id, number, result.session, result.candidate, result.summary, result.evidence, worker=name)
        return self.execute(returned)

    def _execute_locked(self, cmd: Any) -> Event:
        self.ledger.repair_tail()
        events = self.ledger.read()
        records = replay(events)
        seq = events[-1].seq + 1 if events else 1
        prev = stable_hash(events[-1].to_line()) if events else None

        try:
            result = C.run(records, cmd, seq, prev)
            if result.event.kind == Kind.VERIFIED and "review_scope" in result.record.constraints:
                require_review_hook(self.state)
            candidate = events + [result.event]
            rebuilt = replay(candidate)
        except ConflictError:
            # 业务拒绝（状态不允许 / 过期尝试 / 上限）：内存视图保持原样
            self._records = records
            raise
        except Exception:
            self._records = records
            raise
        if rebuilt[result.record.id].status != result.record.status:
            raise ConflictError("事件回放与预期状态不一致，拒绝写入")

        self._commit(result.event)
        self._records = rebuilt
        return result.event

    def _commit(self, event: Event) -> None:
        path = self.ledger.record
        backup = path.read_bytes() if path.exists() else None
        try:
            self.ledger.append([event])
        except OSError:
            if backup is not None:
                path.write_bytes(backup)
            elif path.exists():
                path.unlink()
            self._records = None
            raise

    # ---- 批量 -----------------------------------------------------------------

    def execute_many(self, cmds: list[Any]) -> list[Event]:
        return [self.execute(cmd) for cmd in cmds]

    def reconcile(self) -> dict[str, Any]:
        """检查磁盘上的日志与内存视图是否一致，用于主控重启后自检。"""
        events = self.ledger.read()
        self._records = replay(events)
        return {
            "events": len(events),
            "tasks": len(self._records),
            "last_seq": events[-1].seq if events else 0,
        }

    # ---- 重启后的核对 ---------------------------------------------------------

    def scan(self) -> dict[str, Any]:
        """主控重启后核对：哪些尝试“已派出但还没有结果”、哪些超时待核对、哪些阻塞。

        - ``dispatched_without_result``：状态是执行中、且当前尝试仍在跑；
        - ``timed_out``：超时被判定、还没核对结束的尝试（不能重派）；
        - ``blocked``：等主控处理的任务。
        """
        records = self._load()
        dispatched: list[dict[str, Any]] = []
        timed_out: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for record in sorted(records.values(), key=lambda r: r.id):
            attempt = record.attempt(record.active_attempt)
            item = {
                "task": record.id,
                "attempt": attempt.number if attempt else 0,
                "session": attempt.session if attempt else "",
                "workspace": attempt.workspace if attempt else record.project_root,
                "outputs": list(record.outputs),
                **({"candidate": attempt.candidate} if attempt and attempt.candidate else {}),
            }
            if (
                record.status is TaskStatus.IN_PROGRESS
                and attempt is not None
                and attempt.status == "running"
            ):
                dispatched.append(item)
            elif (
                record.status is TaskStatus.BLOCKED
                and attempt is not None
                and attempt.verdict != "dead"
                and record.constraints.get("failure_kind") == "timeout"
            ):
                timed_out.append({**item, "verdict": attempt.verdict or "pending"})
            elif record.status is TaskStatus.BLOCKED:
                blocked.append(
                    {
                        "task": record.id,
                        "kind": record.constraints.get("failure_kind", ""),
                        "reason": record.constraints.get("failure_reason", ""),
                    }
                )
        return {
            "dispatched_without_result": dispatched,
            "timed_out": timed_out,
            "blocked": blocked,
            "clean": not dispatched and not timed_out,
        }


# ---- 回放 -------------------------------------------------------------------


def replay(events: list[Event]) -> dict[str, TaskRecord]:
    records: dict[str, TaskRecord] = {}
    expected_seq = 1
    for event in events:
        if event.seq != expected_seq:
            raise CorruptionError(
                f"日志序号不连续：期望 {expected_seq}，实际 {event.seq}"
            )
        expected_seq += 1
        apply(records, event)
    return records


def apply(records: dict[str, TaskRecord], event: Event) -> None:
    kind = event.kind
    tid = event.task_id
    data = event.data

    if kind == Kind.TASK_CREATED:
        records[tid] = TaskRecord(
            id=tid,
            status=TaskStatus.PENDING,
            intent=data.get("intent", ""),
            route=data.get("route", ""),
            role=data.get("role", ""),
            project_root=data.get("project_root", ""),
            inputs=list(data.get("inputs") or []),
            outputs=list(data.get("outputs") or []),
            write_scope=list(data.get("write_scope") or []),
            deps=list(data.get("deps") or []),
            constraints=dict(data.get("constraints") or {}),
            created_at=now_utc(),
        )
        return

    if kind == Kind.PLAN_CREATED:
        from ..store.record import PlanRecord

        records[tid] = TaskRecord(
            id=tid,
            intent=f"计划 {tid}",
            created_at=now_utc(),
            plan=PlanRecord(
                id=tid,
                leaves=list(data.get("leaves") or []),
                members=list(data.get("members") or data.get("leaves") or []),
                version=1,
            ),
        )
        return

    record = records.get(tid)
    if record is None:
        raise CorruptionError(f"事件 {event.seq} 引用了未知任务 {tid}")

    if kind == Kind.PLAN_UPDATED:
        from ..store.record import PlanRecord

        plan = record.plan or PlanRecord(id=tid)
        record.plan = PlanRecord(
            id=plan.id,
            leaves=list(data.get("leaves") or []),
            members=list(data.get("members") or data.get("leaves") or []),
            version=int(data.get("version") or plan.version + 1),
            history=list(plan.history)
            + [
                {
                    "version": int(data.get("version") or plan.version + 1),
                    "added": list(data.get("added") or []),
                    "removed": list(data.get("removed") or []),
                }
            ],
        )
        return

    if kind == Kind.DEPENDENCY:
        for dep in data.get("after") or []:
            if dep not in record.deps:
                record.deps.append(dep)
    elif kind in (Kind.ATTEMPT_STARTED, Kind.ATTEMPT_RESUMED):
        from ..store.record import Attempt

        number = int(data["attempt"])
        record.attempts.append(
            Attempt(
                number=number,
                session=data.get("session", ""),
                workspace=data.get("workspace", ""),
                model=data.get("model", ""),
                effort=data.get("effort", ""),
                status="running",
                candidate=data.get("candidate", ""),
                summary=data.get("summary", ""),
            )
        )
        record.active_attempt = number
        record.status = TaskStatus.IN_PROGRESS
    elif kind == Kind.ATTEMPT_RECONCILED:
        attempt = record.attempt(int(data["attempt"]))
        if attempt is None:
            raise CorruptionError(f"事件 {event.seq} 引用了未知尝试")
        attempt.verdict = data.get("verdict", "")
        attempt.reconciled_by = data.get("by", "")
        record.constraints["reconciled"] = attempt.verdict
        if attempt.verdict == "unknown":
            attempt.status = "awaiting_reconciliation"
            record.constraints["failure_kind"] = "timeout"
            record.status = TaskStatus.BLOCKED
        if attempt.verdict == "running":
            attempt.status = "running"
        if attempt.verdict == "dead":
            if attempt.status in ("running", "awaiting_reconciliation"):
                attempt.status = "abandoned"
            if attempt.number == record.active_attempt:
                record.status = TaskStatus.BLOCKED
    elif kind == Kind.EVENT and data.get("kind") == "worker.session":
        payload = data.get("payload", {})
        attempt = record.attempt(payload.get("attempt"))
        if attempt and payload.get("session"):
            attempt.session = payload["session"]
    elif kind == Kind.WORKER_RETURNED:
        attempt = record.attempt(int(data["attempt"]))
        if attempt is None:
            raise CorruptionError(f"事件 {event.seq} 引用了未知尝试")
        timed_out = record.status is TaskStatus.BLOCKED and record.constraints.get("failure_kind") == "timeout"
        pending_timeout = timed_out and attempt.verdict != "dead"
        if record.active_attempt == attempt.number and (record.status is TaskStatus.IN_PROGRESS or pending_timeout):
            attempt.session = data.get("session") or attempt.session
            attempt.candidate = data.get("candidate", "")
            attempt.summary = data.get("summary", "")
    elif kind == Kind.ATTEMPT_REDISPATCHED:
        from ..store.record import Attempt

        number = int(data["attempt"])
        record.attempts.append(
            Attempt(
                number=number,
                session=data.get("session", ""),
                workspace=data.get("workspace", ""),
                model=data.get("model", ""),
                effort=data.get("effort", ""),
                status="running",
            )
        )
        record.active_attempt = number
        record.status = TaskStatus.IN_PROGRESS
        record.constraints.pop("reconciled", None)
        record.constraints.pop("failure_kind", None)
        record.constraints.pop("failure_reason", None)
    elif kind == Kind.ATTEMPT_LATE_RESULT:
        attempt = record.attempt(int(data["attempt"]))
        if attempt is None:
            raise CorruptionError(f"事件 {event.seq} 引用了未知尝试")
        attempt.candidate = data.get("candidate", "")
        attempt.summary = data.get("summary", "")
        attempt.status = "late"
        attempt.ended_at = now_utc()
    elif kind == Kind.ATTEMPT_SUBMITTED:
        attempt = record.attempt(int(data["attempt"]))
        if attempt is None:
            raise CorruptionError(f"事件 {event.seq} 引用了未知尝试")
        attempt.candidate = data.get("candidate", "")
        attempt.summary = data.get("summary", "")
        if attempt.number != record.active_attempt:
            # 旧尝试的迟到结果：只留档，不改任务状态
            attempt.status = "late"
            attempt.ended_at = now_utc()
            return
        attempt.status = "submitted"
        attempt.review_versions = dict(data.get("review_versions", {}))
        if data.get("recovered"):
            attempt.verdict = "recovered"
            attempt.reconciled_by = data["by"]
            record.constraints.pop("failure_kind", None)
            record.constraints.pop("failure_reason", None)
            record.constraints["reconciled"] = "recovered"
        record.status = TaskStatus.VERIFYING
    elif kind == Kind.ATTEMPT_FAILED:
        attempt = record.attempt(int(data["attempt"]))
        if attempt is None:
            # 派单与失败合并成一条事件时，先补出这次尝试
            from ..store.record import Attempt

            attempt = Attempt(
                number=int(data["attempt"]),
                session=data.get("session", ""),
                workspace=record.project_root,
                status="abandoned",
            )
            record.attempts.append(attempt)
            record.active_attempt = attempt.number
        attempt.session = data.get("session") or attempt.session
        attempt.status = "awaiting_reconciliation" if data.get("kind") == "timeout" else "abandoned"
        record.constraints["failure_kind"] = data.get("kind", "")
        record.constraints["failure_reason"] = data.get("reason", "")
        record.status = TaskStatus.BLOCKED
    elif kind in (Kind.VERIFIED, Kind.VERIFICATION_FAILED_FINAL):
        from ..store.record import Step

        outcome = "pass" if kind == Kind.VERIFIED else "rework"
        record.steps.append(
            Step(
                id=data.get("step", ""),
                evidence=data.get("evidence", ""),
                outcome=outcome,
                check=data.get("check", ""),
                notes=data.get("notes", ""),
                attempt=int(data.get("attempt", 0)),
            )
        )
        if outcome == "pass":
            record.accepted_attempt = int(data["attempt"])
            record.status = TaskStatus.ACCEPTED
        else:
            record.status = TaskStatus.BLOCKED
    elif kind == Kind.BLOCKED:
        record.status = TaskStatus.BLOCKED
        if data.get("kind"):
            record.constraints["failure_kind"] = data["kind"]
        if data.get("reason"):
            record.constraints["failure_reason"] = data["reason"]
    elif kind == Kind.TASK_CANCELLED:
        record.status = TaskStatus.CANCELLED
    elif kind == Kind.TASK_RESUMED:
        record.status = (
            TaskStatus.IN_PROGRESS if record.active_attempt else TaskStatus.PENDING
        )
    elif kind == Kind.ARTIFACT:
        from ..store.record import Artifact

        record.artifacts[data["name"]] = Artifact(
            name=data["name"],
            location=data.get("location", ""),
            version=data.get("version", "1"),
            created_at=now_utc(),
            hash=data.get("hash", ""),
            created_by=data.get("created_by", ""),
            role=data.get("role", ""),
        )
    elif kind == Kind.ARTIFACT_BOUND:
        record.evidence[data["step"]] = (record.active_attempt, data["artifact"])
        artifact = record.artifacts.get(data["artifact"])
        if artifact is not None:
            artifact.bound_to = data["step"]
    # NOTE / EVENT / INTEGRATION / TASK_LANDED 只记录事实，不改状态


__all__ = ["Coordinator", "CorruptionError", "file_lock", "replay", "apply"]
