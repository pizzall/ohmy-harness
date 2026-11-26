"""命令处理器：命令 → 校验 → 新记录 → 新事件。

单次变更：先校验命令对当前记录是否成立，再产生“新记录 + 新事件”。
任何校验失败都在写入之前抛出，因此不会留下半截记录。真正的提交（序号、prev 链、
文件写入与 fsync）由 :mod:`ohmyagent.ledger` 完成。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from ..core.errors import (
    ConflictError,
    DuplicateTaskError,
    OhMyAgentError,
    UnknownTaskError,
)
from ..core.lifecycle import DEFAULT_LIMITS, Limits, TaskStatus
from ..util.clock import now_utc
from .record import Artifact, Attempt, Event, Kind, PlanRecord, Step, TaskRecord
from .review import REVIEW_ROLES, check_coverage, file_hash, review_versions, validate_scope

OK_STATUSES = frozenset({"pass", "accepted"})
REWORK_STATUSES = frozenset({"rework", "fail", "failed"})


# ---- 命令定义 ---------------------------------------------------------------


@dataclass(frozen=True)
class CreateTask:
    task_id: str
    intent: str
    project_root: str
    route: str = ""
    role: str = ""
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    deps: tuple[str, ...] = ()
    constraints: dict[str, Any] | None = None
    priority: str = "normal"
    budget: dict[str, Any] | None = None


@dataclass(frozen=True)
class LinkDependency:
    task_id: str
    after: tuple[str, ...] = ()
    concurrent_write: tuple[str, ...] = ()


@dataclass(frozen=True)
class DispatchTask:
    """把任务派给一个 Worker（开始第 1 次尝试）。

    - 给了 ``use_worker``：由主控在这里调用 Worker，成功后把原生会话 ID 记进尝试；
      失败时按 ``WorkerError.kind`` 把任务打到阻塞，并把失败证据写进 constraints；
    - 否则按调用方给出的 ``session`` 登记（人工派单 / 外部启动）。
    """

    task_id: str
    session: str
    workspace: str = ""
    model: str = ""
    effort: str = ""
    limits: Limits = DEFAULT_LIMITS
    force: bool = False
    use_worker: Any = None
    timeout: float | None = None


@dataclass(frozen=True)
class WorkerReturned:
    task_id: str
    attempt: int
    session: str
    candidate: str = ""
    summary: str = ""
    evidence: str = ""
    kind: str = ""
    reason: str = ""
    worker: str = ""


@dataclass(frozen=True)
class BeginAttempt:
    """返工：在同样的任务上开始下一次尝试（保留原会话信息）。"""

    task_id: str
    session: str
    workspace: str = ""
    model: str = ""
    effort: str = ""
    limits: Limits = DEFAULT_LIMITS


@dataclass(frozen=True)
class ReconcileAttempt:
    """超时后的核对。

    - ``running``：旧尝试仍在跑，保留“待核对”，不允许重派；
    - ``unknown``：观察超时或断连，保留尝试和写范围，等待核对；
    - ``recovered``：已从原尝试收回全部预期产物，转入待验证；
    - ``dead``：确认旧尝试已结束，允许重派，旧尝试的迟到结果只留档。
    """

    task_id: str
    attempt: int
    verdict: str = "running"
    by: str = ""
    candidate: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class Redispatch:
    task_id: str
    session: str
    workspace: str = ""
    model: str = ""
    effort: str = ""


@dataclass(frozen=True)
class SubmitResult:
    task_id: str
    attempt: int
    candidate: str
    summary: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class Verify:
    task_id: str
    outcome: str
    check: str = ""
    evidence: str = ""
    notes: str = ""
    attempt: int | None = None
    review_evidence: tuple[dict, ...] = ()


@dataclass(frozen=True)
class DecideBlock:
    task_id: str
    kind: str = "environment"
    reason: str = ""
    step: str = ""


@dataclass(frozen=True)
class CancelTask:
    task_id: str
    reason: str = ""


@dataclass(frozen=True)
class ResumeTask:
    task_id: str
    reason: str = ""


@dataclass(frozen=True)
class AddNote:
    task_id: str
    text: str


@dataclass(frozen=True)
class RegisterArtifact:
    task_id: str
    name: str
    location: str
    version: str = "1"
    hash: str = ""
    created_by: str = ""
    role: str = ""


@dataclass(frozen=True)
class BindArtifact:
    task_id: str
    step: str
    artifact: str


@dataclass(frozen=True)
class RecordEvent:
    task_id: str
    kind: str
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class DecideLanding:
    task_id: str
    outcome: str = "accepted"
    by: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ConfirmIntegrating:
    task_id: str


@dataclass(frozen=True)
class CreatePlan:
    """登记一个计划版本，写明本轮纳入的（叶子）任务。"""

    task_id: str
    leaf_tasks: tuple[str, ...] = ()
    members: tuple[str, ...] = ()


@dataclass(frozen=True)
class UpdatePlan:
    """调整计划成员：新增或移出的叶子任务要记下差异。"""

    task_id: str
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    leaves: tuple[str, ...] | None = None
    reason: str = ""


# ---- 结果 -------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    record: TaskRecord
    event: Event


# ---- 辅助 -------------------------------------------------------------------


def _find(records: dict[str, TaskRecord], task_id: str) -> TaskRecord:
    record = records.get(task_id)
    if record is None:
        raise UnknownTaskError(task_id)
    return record


def _step(record: TaskRecord, step_id: str) -> Step:
    for step in record.steps:
        if step.id == step_id:
            return step
    raise ConflictError(f"任务 {record.id} 没有步骤 {step_id}")


def _attempt(record: TaskRecord, number: int) -> Attempt:
    obj = record.attempt(number)
    if obj is None:
        raise ConflictError(f"任务 {record.id} 没有尝试 {number}")
    return obj


def _expected(record: TaskRecord, status: TaskStatus, action: str) -> None:
    if record.status is not status:
        raise ConflictError(
            f"任务 {record.id} 处于 {record.status.value}，不能{action}"
        )


def _auto_step_id(record: TaskRecord) -> str:
    taken = {s.id for s in record.steps}
    k = len(taken) + 1
    base = f"verify-{k}"
    while base in taken:
        k += 1
        base = f"verify-{k}"
    return base


def _copy(record: TaskRecord) -> TaskRecord:
    return replace(
        record,
        inputs=list(record.inputs),
        outputs=list(record.outputs),
        write_scope=list(record.write_scope),
        deps=list(record.deps),
        constraints=dict(record.constraints),
        steps=list(record.steps),
        attempts=list(record.attempts),
        artifacts=dict(record.artifacts),
        evidence=dict(record.evidence),
    )

# ---- 写范围与上限 -----------------------------------------------------------


def _norm_scope(path: str) -> str:
    p = PurePosixPath(path.strip().replace("\\", "/"))
    while str(p).startswith("./"):
        p = PurePosixPath(str(p)[2:])
    return str(p).rstrip("/")


def overlaps(a: Iterable[str], b: Iterable[str]) -> list[tuple[str, str]]:
    """返回两组写范围里互相包含、重叠的路径对（空列表表示可以并行写）。"""
    conflicts: list[tuple[str, str]] = []
    left = [_norm_scope(x) for x in a if x]
    right = [_norm_scope(x) for x in b if x]
    for x in left:
        for y in right:
            if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                conflicts.append((x, y))
    return conflicts


def _running_writers(
    records: dict[str, TaskRecord], exclude: str, roots: list[str]
) -> list[TaskRecord]:
    writers = []
    for other in records.values():
        active = other.attempt(other.active_attempt)
        timed_out = other.status is TaskStatus.BLOCKED and other.constraints.get("failure_kind") == "timeout"
        unresolved = timed_out and active and active.verdict != "dead"
        if other.id == exclude or (other.status is not TaskStatus.IN_PROGRESS and not unresolved):
            continue
        if other.project_root not in roots:
            continue  # 不同项目根目录之间互不影响
        writers.append(other)
    return writers


def _check_parallel_write(
    records: dict[str, TaskRecord], record: TaskRecord, force: bool = False
) -> None:
    if force or not record.write_scope:
        return
    roots = [record.project_root]
    for other in _running_writers(records, record.id, roots):
        clash = overlaps(record.write_scope, other.write_scope)
        if clash:
            a, b = clash[0]
            raise ConflictError(
                f"任务 {record.id} 的写范围 {a} 与正在执行的 {other.id} 的 {b} 重叠，"
                "请改用独立工作树或等待对方验收"
            )


def _check_limits(record: TaskRecord, limits: Limits) -> None:
    if not isinstance(limits, Limits):
        # 命令来自其它进程时，默认值可能只是 pickle 出来的元组，不能因此放过上限
        limits = DEFAULT_LIMITS
    if record.number >= limits.max_attempts:
        raise ConflictError(
            f"任务 {record.id} 已用满 {limits.max_attempts} 次尝试，"
            "请保留最后证据并交由主控处理"
        )


def _ensure_active_attempt(record: TaskRecord, attempt: int, action: str) -> Attempt:
    """只有当前有效尝试可以改变状态；旧尝试的迟到结果一律拒绝。"""
    if attempt != record.active_attempt:
        raise ConflictError(
            f"任务 {record.id} 当前有效尝试是 {record.active_attempt}，"
            f"拒绝了来自旧尝试 {attempt} 的{action}"
        )
    return _attempt(record, attempt)


# ---- 各个命令 ---------------------------------------------------------------


def _create_task(
    records: dict[str, TaskRecord], cmd: CreateTask, seq: int, prev: str | None
) -> CommandResult:
    if cmd.task_id in records:
        raise DuplicateTaskError(f"任务 {cmd.task_id} 已存在")
    constraints = cmd.constraints or {}
    if cmd.role.strip().casefold() in REVIEW_ROLES or "review_scope" in constraints:
        validate_scope(constraints.get("review_scope"))
    record = TaskRecord(
        id=cmd.task_id,
        status=TaskStatus.PENDING,
        intent=cmd.intent,
        route=cmd.route,
        role=cmd.role,
        project_root=cmd.project_root,
        inputs=list(cmd.inputs),
        outputs=list(cmd.outputs),
        write_scope=list(cmd.write_scope),
        deps=list(cmd.deps),
        constraints=dict(cmd.constraints or {}),
        created_at=now_utc(),
    )
    event = Event(
        seq=seq,
        kind=Kind.TASK_CREATED,
        task_id=cmd.task_id,
        data={
            "intent": cmd.intent,
            "project_root": cmd.project_root,
            "route": cmd.route,
            "role": cmd.role,
            "inputs": list(cmd.inputs),
            "outputs": list(cmd.outputs),
            "write_scope": list(cmd.write_scope),
            "deps": list(cmd.deps),
            "constraints": dict(cmd.constraints or {}),
        },
        prev=prev,
    )
    return CommandResult(replace(record), event)


def _link_dependencies(
    records: dict[str, TaskRecord], cmd: LinkDependency, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    new = _copy(record)
    for dep in cmd.after:
        if dep not in new.deps:
            new.deps.append(dep)
    event = Event(
        seq=seq,
        kind=Kind.DEPENDENCY,
        task_id=cmd.task_id,
        data={
            "after": list(cmd.after),
            "concurrent_write": list(cmd.concurrent_write),
        },
        prev=prev,
    )
    return CommandResult(new, event)


def _start_attempt(
    record: TaskRecord,
    *,
    number: int,
    session: str,
    workspace: str,
    model: str,
    effort: str,
) -> TaskRecord:
    attempt = Attempt(
        number=number,
        session=session,
        workspace=str(Path(workspace or record.project_root).resolve()),
        model=model,
        effort=effort,
        status="running",
        started_at=now_utc(),
    )
    new = _copy(record)
    new.status = TaskStatus.IN_PROGRESS
    new.active_attempt = number
    new.attempts.append(attempt)
    return new


def _dispatch(
    records: dict[str, TaskRecord], cmd: DispatchTask, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.status.is_terminal:
        raise ConflictError(f"任务 {record.id} 已取消，不能派单")
    if record.status is not TaskStatus.PENDING:
        raise ConflictError(
            f"任务 {record.id} 处于 {record.status.value}，不能派单"
        )
    _check_limits(record, cmd.limits)
    _check_parallel_write(records, record, force=cmd.force)
    number = record.number + 1
    if cmd.use_worker is not None:
        raise ConflictError("直调 Worker 需要通过 Coordinator 登记并启动")
    new = _start_attempt(
        record,
        number=number,
        session=cmd.session,
        workspace=cmd.workspace,
        model=cmd.model,
        effort=cmd.effort,
    )
    event = Event(
        seq=seq,
        kind=Kind.ATTEMPT_STARTED,
        task_id=cmd.task_id,
        data={
            "attempt": number,
            "session": cmd.session,
            "workspace": new.attempts[-1].workspace,
            "model": cmd.model,
            "effort": cmd.effort,
        },
        prev=prev,
    )
    return CommandResult(new, event)


def _worker_returned(records: dict[str, TaskRecord], cmd: WorkerReturned, seq: int, prev: str | None) -> CommandResult:
    record = _find(records, cmd.task_id)
    attempt = _attempt(record, cmd.attempt)
    new = _copy(record)
    data = {
        "attempt": cmd.attempt, "session": cmd.session or attempt.session, "workspace": attempt.workspace,
        "model": attempt.model, "effort": attempt.effort, "candidate": cmd.candidate, "summary": cmd.summary,
        "evidence": cmd.evidence, "kind": cmd.kind, "reason": cmd.reason, "worker": cmd.worker,
    }
    kind = Kind.WORKER_RETURNED
    if cmd.kind and record.active_attempt == cmd.attempt and record.status is TaskStatus.IN_PROGRESS and attempt.status == "running":
        kind = Kind.ATTEMPT_FAILED
        new.status = TaskStatus.BLOCKED
    return CommandResult(new, Event(seq, kind, record.id, data, prev))


def _begin_attempt(
    records: dict[str, TaskRecord], cmd: BeginAttempt, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.status.is_terminal:
        raise ConflictError(f"任务 {record.id} 已取消，不能返工")
    if record.status not in (TaskStatus.BLOCKED, TaskStatus.PENDING):
        raise ConflictError(
            f"任务 {record.id} 处于 {record.status.value}，不能开始新尝试"
        )
    if record.status is TaskStatus.BLOCKED and record.constraints.get("failure_kind") == "timeout":
        raise ConflictError("超时请先核对原尝试，再收件或重派")
    _check_limits(record, cmd.limits)
    _check_parallel_write(records, record)
    number = record.number + 1
    new = _start_attempt(
        record,
        number=number,
        session=cmd.session,
        workspace=cmd.workspace,
        model=cmd.model,
        effort=cmd.effort,
    )
    previous = record.attempts[-1] if record.attempts else None
    event = Event(
        seq=seq,
        kind=Kind.ATTEMPT_RESUMED,
        task_id=cmd.task_id,
        data={
            "attempt": number,
            "replaces": previous.number if previous else 0,
            "session": cmd.session,
            "workspace": new.attempts[-1].workspace,
            "model": cmd.model,
            "effort": cmd.effort,
        },
        prev=prev,
    )
    return CommandResult(new, event)


def _submit(
    records: dict[str, TaskRecord], cmd: SubmitResult, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.status.is_terminal:
        raise ConflictError(f"任务 {record.id} 已取消，迟到的结果不再改变状态")
    if cmd.attempt == record.active_attempt and record.status is not TaskStatus.IN_PROGRESS:
        raise ConflictError(f"任务 {record.id} 处于 {record.status.value}，不能提交结果")
    if "review_scope" in record.constraints and not cmd.candidate.strip():
        raise ConflictError("审查需要可定位的候选版本")
    attempt = _attempt(record, cmd.attempt)
    new = _copy(record)
    target = new.attempts[new.attempts.index(attempt)]
    target.candidate = cmd.candidate
    target.summary = cmd.summary
    target.ended_at = now_utc()
    if cmd.attempt != record.active_attempt or record.status is not TaskStatus.IN_PROGRESS:
        # 旧尝试的迟到结果：只留档，不改任务状态
        target.status = "late"
        event = Event(
            seq=seq,
            kind=Kind.ATTEMPT_LATE_RESULT,
            task_id=cmd.task_id,
            data={
                "attempt": attempt.number,
                "candidate": cmd.candidate,
                "summary": cmd.summary,
            },
            prev=prev,
        )
        return CommandResult(new, event)
    target.status = "submitted"
    new.status = TaskStatus.VERIFYING
    versions = review_versions(record, attempt)
    event = Event(
        seq=seq,
        kind=Kind.ATTEMPT_SUBMITTED,
        task_id=cmd.task_id,
        data={
            "attempt": attempt.number,
            "candidate": cmd.candidate,
            "summary": cmd.summary,
            "evidence": cmd.evidence,
            **({"review_versions": versions} if versions else {}),
        },
        prev=prev,
    )
    return CommandResult(new, event)


def _verify(
    records: dict[str, TaskRecord], cmd: Verify, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.status.is_terminal:
        raise ConflictError(f"任务 {record.id} 已取消，迟到的验收不再改变状态")
    _expected(record, TaskStatus.VERIFYING, "验收")
    number = record.active_attempt if cmd.attempt is None else cmd.attempt
    attempt = _ensure_active_attempt(record, number, "验收")
    if attempt.status != "submitted":
        raise ConflictError(f"尝试 {attempt.number} 还没提交，不能验收")

    outcome = cmd.outcome
    if outcome in OK_STATUSES:
        outcome = "pass"
    elif outcome in REWORK_STATUSES:
        outcome = "rework"
    elif outcome != "blocked":
        raise ConflictError(f"未知验收结论 {cmd.outcome}")

    review_items = []
    if outcome == "pass" and ("review_scope" in record.constraints or record.role.strip().casefold() in REVIEW_ROLES):
        if "review_scope" not in record.constraints:
            raise ConflictError("旧审查任务缺少范围，请按原要求建有范围的审查任务")
        review_items = check_coverage(record, attempt, cmd.review_evidence)
    elif cmd.review_evidence:
        raise ConflictError("逐项放行证据只能提交给已冻结范围的通过申请")

    step = Step(
        id=_auto_step_id(record),
        evidence=cmd.evidence,
        outcome=outcome,
        check=cmd.check,
        notes=cmd.notes,
        attempt=attempt.number,
    )
    new = _copy(record)
    new.steps.append(step)
    if outcome == "pass":
        new.status = TaskStatus.ACCEPTED
        new.accepted_attempt = attempt.number
    else:
        new.status = TaskStatus.BLOCKED
        if not cmd.evidence:
            new.steps[-1].evidence = "report"
    event = Event(
        seq=seq,
        kind=Kind.VERIFIED if outcome == "pass" else Kind.VERIFICATION_FAILED_FINAL,
        task_id=cmd.task_id,
        data={
            "attempt": attempt.number,
            "outcome": outcome,
            "step": step.id,
            "check": cmd.check,
            "evidence": step.evidence,
            "notes": cmd.notes,
            **({"review_items": review_items} if review_items else {}),
        },
        prev=prev,
    )
    return CommandResult(new, event)


def _decide_block(
    records: dict[str, TaskRecord], cmd: DecideBlock, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.status.is_terminal:
        raise ConflictError(f"任务 {record.id} 已取消，不能改为阻塞")
    new = _copy(record)
    new.status = TaskStatus.BLOCKED
    event = Event(
        seq=seq,
        kind=Kind.BLOCKED,
        task_id=cmd.task_id,
        data={"kind": cmd.kind, "reason": cmd.reason, "step": cmd.step},
        prev=prev,
    )
    return CommandResult(new, event)


def _cancel(
    records: dict[str, TaskRecord], cmd: CancelTask, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.status.is_terminal:
        raise ConflictError(f"任务 {record.id} 已经取消")
    new = _copy(record)
    new.status = TaskStatus.CANCELLED
    for attempt in new.attempts:
        if attempt.status == "running":
            attempt.status = "abandoned"
    event = Event(
        seq=seq,
        kind=Kind.TASK_CANCELLED,
        task_id=cmd.task_id,
        data={"reason": cmd.reason},
        prev=prev,
    )
    return CommandResult(new, event)


def _resume(
    records: dict[str, TaskRecord], cmd: ResumeTask, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.status is not TaskStatus.BLOCKED:
        raise ConflictError(
            f"任务 {record.id} 处于 {record.status.value}，不能恢复"
        )
    if record.constraints.get("failure_kind") == "timeout":
        raise ConflictError("超时请核对原尝试，再收件或重派")
    new = _copy(record)
    new.status = TaskStatus.IN_PROGRESS if record.active_attempt else TaskStatus.PENDING
    event = Event(
        seq=seq,
        kind=Kind.TASK_RESUMED,
        task_id=cmd.task_id,
        data={"reason": cmd.reason},
        prev=prev,
    )
    return CommandResult(new, event)


def _note(
    records: dict[str, TaskRecord], cmd: AddNote, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.status.is_terminal:
        raise ConflictError(f"任务 {record.id} 已取消，不再追加过程记录")
    event = Event(
        seq=seq,
        kind=Kind.NOTE,
        task_id=cmd.task_id,
        data={"text": cmd.text},
        prev=prev,
    )
    return CommandResult(_copy(record), event)


def _register_artifact(
    records: dict[str, TaskRecord], cmd: RegisterArtifact, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    artifact = Artifact(
        name=cmd.name,
        location=cmd.location,
        version=cmd.version,
        created_at=now_utc(),
        hash=cmd.hash,
        created_by=cmd.created_by or record.id,
        role=cmd.role,
    )
    new = _copy(record)
    new.artifacts[cmd.name] = artifact
    event = Event(
        seq=seq,
        kind=Kind.ARTIFACT,
        task_id=cmd.task_id,
        data={
            "name": cmd.name,
            "location": cmd.location,
            "version": cmd.version,
            "hash": cmd.hash,
            "created_by": artifact.created_by,
            "role": cmd.role,
        },
        prev=prev,
    )
    return CommandResult(new, event)


def _bind_artifact(
    records: dict[str, TaskRecord], cmd: BindArtifact, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    step = _step(record, cmd.step)
    if cmd.artifact not in record.artifacts:
        raise ConflictError(f"任务 {record.id} 没有产物 {cmd.artifact}")
    new = _copy(record)
    new.evidence[cmd.step] = (new.active_attempt, cmd.artifact)
    new.artifacts[cmd.artifact].bound_to = step.id
    event = Event(
        seq=seq,
        kind=Kind.ARTIFACT_BOUND,
        task_id=cmd.task_id,
        data={"step": cmd.step, "artifact": cmd.artifact},
        prev=prev,
    )
    return CommandResult(new, event)


def _record_event(
    records: dict[str, TaskRecord], cmd: RecordEvent, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    event = Event(
        seq=seq,
        kind=Kind.EVENT,
        task_id=cmd.task_id,
        data={"kind": cmd.kind, "payload": dict(cmd.payload or {})},
        prev=prev,
    )
    return CommandResult(_copy(record), event)


def _decide_landing(
    records: dict[str, TaskRecord], cmd: DecideLanding, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if cmd.outcome not in ("accepted", "rework", "blocked"):
        raise ConflictError(f"未知落地结论 {cmd.outcome}")
    if cmd.outcome == "accepted" and "review_scope" in record.constraints and record.status is not TaskStatus.ACCEPTED:
        raise ConflictError("审查范围尚未通过核验，不能落地")
    event = Event(
        seq=seq,
        kind=Kind.TASK_LANDED,
        task_id=cmd.task_id,
        data={"outcome": cmd.outcome, "by": cmd.by, "reason": cmd.reason},
        prev=prev,
    )
    return CommandResult(_copy(record), event)


def _confirm_integrating(
    records: dict[str, TaskRecord], cmd: ConfirmIntegrating, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if "review_scope" in record.constraints and record.status is not TaskStatus.ACCEPTED:
        raise ConflictError("审查范围尚未通过核验，不能确认集成")
    event = Event(
        seq=seq,
        kind=Kind.INTEGRATION,
        task_id=cmd.task_id,
        data={"confirmed": True},
        prev=prev,
    )
    return CommandResult(_copy(record), event)


# ---- 分派表 ---------------------------------------------------------------

def _create_plan(
    records: dict[str, TaskRecord], cmd: CreatePlan, seq: int, prev: str | None
) -> CommandResult:
    if cmd.task_id in records:
        raise DuplicateTaskError(f"计划 {cmd.task_id} 已存在")
    leaves = list(dict.fromkeys(cmd.leaf_tasks or cmd.members))
    members = list(dict.fromkeys(cmd.members or cmd.leaf_tasks))
    if not members:
        raise ConflictError(f"计划 {cmd.task_id} 没有纳入任何任务")
    record = TaskRecord(
        id=cmd.task_id,
        intent=f"计划 {cmd.task_id}",
        created_at=now_utc(),
        plan=PlanRecord(id=cmd.task_id, leaves=leaves, members=members, version=1),
    )
    event = Event(
        seq=seq,
        kind=Kind.PLAN_CREATED,
        task_id=cmd.task_id,
        data={"leaves": leaves, "members": members},
        prev=prev,
    )
    return CommandResult(record, event)


def _update_plan(
    records: dict[str, TaskRecord], cmd: UpdatePlan, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.plan is None:
        raise ConflictError(f"任务 {record.id} 不是计划")
    current = list(record.plan.leaves)
    current_members = list(record.plan.members)
    members = [m for m in current_members if m not in set(cmd.remove)]
    members += [m for m in cmd.add if m not in members]
    if cmd.leaves is not None:
        target = list(dict.fromkeys(cmd.leaves))
    else:
        target = [l for l in current if l not in set(cmd.remove)]
        target += [l for l in cmd.add if l not in target]
    added = [l for l in target if l not in current]
    removed = [l for l in current if l not in target]
    if not added and not removed:
        raise ConflictError(f"计划 {record.id} 没有变化")
    new = _copy(record)
    assert new.plan is not None
    new.plan = PlanRecord(
        id=record.plan.id,
        leaves=target,
        members=members,
        version=record.plan.version + 1,
        history=list(record.plan.history)
        + [{"version": record.plan.version + 1, "added": added, "removed": removed}],
    )
    event = Event(
        seq=seq,
        kind=Kind.PLAN_UPDATED,
        task_id=cmd.task_id,
        data={
            "version": new.plan.version,
            "leaves": target,
            "members": members,
            "added": added,
            "removed": removed,
            "reason": cmd.reason,
        },
        prev=prev,
    )
    return CommandResult(new, event)


def _reconcile_attempt(
    records: dict[str, TaskRecord], cmd: ReconcileAttempt, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.status.is_terminal:
        raise ConflictError(f"任务 {record.id} 已取消，不再核对旧尝试")
    attempt = _attempt(record, cmd.attempt)
    if cmd.verdict not in ("unknown", "running", "dead", "recovered"):
        raise ConflictError(
            f"任务 {record.id} 的核对结论 {cmd.verdict} 与当前状态不一致，拒绝写入"
        )
    if attempt.number != record.active_attempt:
        raise ConflictError(
            f"任务 {record.id} 当前有效尝试是 {record.active_attempt}，"
            f"不能核对待核对的旧尝试 {attempt.number}"
        )
    if record.status is not TaskStatus.IN_PROGRESS and not (
        record.status is TaskStatus.BLOCKED and record.constraints.get("failure_kind") == "timeout"
    ):
        raise ConflictError("仅可核对在途或超时待核对的尝试")
    if cmd.verdict == "recovered":
        if not cmd.candidate.strip() or not cmd.evidence.strip() or not cmd.by.strip():
            raise ConflictError("收回产物需要候选路径、原尝试的来源证据和核对人")
        if record.outputs and cmd.candidate not in record.outputs:
            raise ConflictError("收回的候选不在原任务的预期产物内")
        outputs = {name: file_hash(attempt.workspace, name) for name in record.outputs or [cmd.candidate]}
        try:
            receipt = json.loads((Path(attempt.workspace) / cmd.evidence).read_text(encoding="utf-8-sig"))
        except (ValueError, OSError) as exc:
            raise ConflictError("需要可读的原尝试收件凭据 JSON") from exc
        expected = {"task": record.id, "attempt": attempt.number, "session": attempt.session, "outputs": outputs}
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items()):
            raise ConflictError("收件凭据与原任务、尝试、会话或产物版本不符")
        if not isinstance(receipt.get("log"), str) or not receipt["log"].strip():
            raise ConflictError("收件凭据需指向可回读的原会话或运行日志")
        log_hash = file_hash(attempt.workspace, receipt["log"])
        versions = review_versions(record, attempt)
        data = {
            "attempt": attempt.number, "candidate": cmd.candidate, "evidence": cmd.evidence, "by": cmd.by,
            "output_hashes": outputs, "log": receipt["log"], "log_sha256": log_hash, "recovered": True,
            **({"review_versions": versions} if versions else {}),
        }
        new = replace(record, status=TaskStatus.VERIFYING)
        return CommandResult(new, Event(seq, Kind.ATTEMPT_SUBMITTED, record.id, data, prev))
    new = _copy(record)
    target = new.attempts[new.attempts.index(attempt)]
    target.verdict = cmd.verdict
    target.reconciled_at = now_utc()
    target.reconciled_by = cmd.by
    new.constraints["reconciled"] = cmd.verdict
    if cmd.verdict == "unknown":
        new.status = TaskStatus.BLOCKED
    if cmd.verdict == "dead" and target.status == "running":
        target.status = "abandoned"
        target.ended_at = target.reconciled_at
        new.status = TaskStatus.BLOCKED
    event = Event(
        seq=seq,
        kind=Kind.ATTEMPT_RECONCILED,
        task_id=cmd.task_id,
        data={
            "attempt": attempt.number,
            "verdict": cmd.verdict,
            "by": cmd.by,
        },
        prev=prev,
    )
    return CommandResult(new, event)


def _redispatch(
    records: dict[str, TaskRecord], cmd: Redispatch, seq: int, prev: str | None
) -> CommandResult:
    record = _find(records, cmd.task_id)
    if record.status.is_terminal:
        raise ConflictError(f"任务 {record.id} 已取消，不能重派")
    if record.status is not TaskStatus.BLOCKED:
        raise ConflictError(
            f"任务 {record.id} 处于 {record.status.value}，只有阻塞（待核对）的任务可以重派"
        )
    active = record.attempt(record.active_attempt)
    if active is None or active.verdict != "dead":
        raise ConflictError(
            f"任务 {record.id} 的旧尝试 {record.active_attempt} 还没有确认结束，"
            "不能重派（先核对原会话/进程/工作区）"
        )
    _check_parallel_write(records, record)
    number = record.number + 1
    new = _start_attempt(
        record,
        number=number,
        session=cmd.session,
        workspace=cmd.workspace,
        model=cmd.model,
        effort=cmd.effort,
    )
    event = Event(
        seq=seq,
        kind=Kind.ATTEMPT_REDISPATCHED,
        task_id=cmd.task_id,
        data={
            "attempt": number,
            "replaces": active.number,
            "session": cmd.session,
            "workspace": new.attempts[-1].workspace,
            "model": cmd.model,
            "effort": cmd.effort,
        },
        prev=prev,
    )
    return CommandResult(new, event)


_HANDLERS: dict[type, Callable[[dict[str, TaskRecord], Any, int, str | None], CommandResult]] = {
    CreateTask: _create_task,
    LinkDependency: _link_dependencies,
    DispatchTask: _dispatch,
    WorkerReturned: _worker_returned,
    BeginAttempt: _begin_attempt,
    SubmitResult: _submit,
    Verify: _verify,
    DecideBlock: _decide_block,
    CancelTask: _cancel,
    ResumeTask: _resume,
    AddNote: _note,
    RegisterArtifact: _register_artifact,
    BindArtifact: _bind_artifact,
    RecordEvent: _record_event,
    DecideLanding: _decide_landing,
    ConfirmIntegrating: _confirm_integrating,
    CreatePlan: _create_plan,
    UpdatePlan: _update_plan,
    ReconcileAttempt: _reconcile_attempt,
    Redispatch: _redispatch,
}


def run(
    records: dict[str, TaskRecord], cmd: Any, seq: int, prev: str | None
) -> CommandResult:
    """校验并返回命令结果；校验失败时抛出 OhMyAgentError 的子类。"""
    handler = _HANDLERS.get(type(cmd))
    if handler is None:
        raise OhMyAgentError(f"未知命令 {type(cmd).__name__}")
    return handler(records, cmd, seq, prev)
