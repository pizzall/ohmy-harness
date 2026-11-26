"""任务记录与事件模型。

记录文件是任务项目根目录下 `.superagent/record.txt`，每条事件一行 JSON，
带连续序号和 `prev` 链；事件回放得到 :class:`TaskRecord`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.lifecycle import ATTEMPT_STATUSES, TaskStatus


class Kind:
    """事件种类。"""

    TASK_CREATED = "task.created"
    TASK_CANCELLED = "task.cancelled"
    TASK_RESUMED = "task.resumed"
    ATTEMPT_STARTED = "attempt.started"
    ATTEMPT_SUBMITTED = "attempt.submitted"
    ATTEMPT_LATE_RESULT = "attempt.late_result"
    ATTEMPT_RESUMED = "attempt.resumed"
    VERIFIED = "task.verified"
    VERIFICATION_FAILED_FINAL = "task.verification_failed_final"
    BLOCKED = "task.blocked"
    NOTE = "task.note"
    DEPENDENCY = "task.dependency"
    ARTIFACT = "artifact.registered"
    ARTIFACT_BOUND = "artifact.bound"
    INTEGRATION = "integration.recorded"
    EVENT = "event.recorded"
    TASK_LANDED = "task.landed"
    PLAN_CREATED = "plan.created"
    PLAN_UPDATED = "plan.updated"
    ATTEMPT_FAILED = "attempt.failed"
    WORKER_RETURNED = "worker.returned"
    ATTEMPT_RECONCILED = "attempt.reconciled"
    ATTEMPT_REDISPATCHED = "attempt.redispatched"
    WORKER_DISPATCHED = "worker.dispatched"


CREATE_COMMANDS = frozenset(
    {Kind.TASK_CREATED, Kind.ATTEMPT_STARTED, Kind.DEPENDENCY, Kind.ARTIFACT}
)
"""只有这些事件可以由主控包一层创建任务；其余事件都必须落在已存在的任务上。"""


# ---- 数据结构 ---------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    step: int
    artifact: str


@dataclass
class Artifact:
    name: str
    location: str
    version: str
    created_at: str
    hash: str = ""
    created_by: str = ""
    bound_to: str = ""
    role: str = ""


@dataclass
class Attempt:
    number: int
    session: str
    workspace: str
    model: str = ""
    effort: str = ""
    status: str = "running"
    started_at: str = ""
    ended_at: str = ""
    candidate: str = ""
    summary: str = ""
    verdict: str = ""  # 超时后的核对结论：未核对 / running / dead
    reconciled_at: str = ""
    reconciled_by: str = ""
    review_versions: dict[str, str] = field(default_factory=dict)


@dataclass
class Step:
    id: str
    intent: str = ""
    route: str = ""
    evidence: str = ""
    outcome: str = ""
    check: str = ""
    notes: str = ""
    attempt: int = 0


@dataclass
class Event:
    seq: int
    kind: str
    task_id: str
    data: dict[str, Any] = field(default_factory=dict)
    prev: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "task": self.task_id,
            "prev": self.prev,
            "data": self.data,
        }

    def to_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


@dataclass
class TaskRecord:
    """一个任务的全部事实，由事件回放得到。"""

    id: str
    status: TaskStatus = TaskStatus.PENDING
    intent: str = ""
    route: str = ""
    role: str = ""
    project_root: str = ""
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    write_scope: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    evidence: dict[str, tuple[int, str]] = field(default_factory=dict)
    active_attempt: int = 0
    accepted_attempt: int | None = None
    created_at: str = ""
    plan: PlanRecord | None = None

    @property
    def number(self) -> int:
        return len(self.attempts)

    def attempt(self, number: int) -> Attempt | None:
        for a in self.attempts:
            if a.number == number:
                return a
        return None

    def evidence_for(self, step_id: str) -> EvidenceRef | None:
        ref = self.evidence.get(step_id)
        if ref is None:
            return None
        return EvidenceRef(step=ref[0], artifact=ref[1])


@dataclass(frozen=True)
class TaskSnapshot:
    """对外只读视图，避免调用方修改内部记录。"""

    id: str
    status: TaskStatus
    intent: str
    route: str
    role: str
    project_root: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    write_scope: tuple[str, ...]
    steps: tuple[Step, ...]
    attempts: tuple[Attempt, ...]
    active_attempt: int
    accepted_attempt: int | None

    @classmethod
    def of(cls, record: TaskRecord) -> TaskSnapshot:
        return cls(
            id=record.id,
            status=record.status,
            intent=record.intent,
            route=record.route,
            role=record.role,
            project_root=record.project_root,
            inputs=tuple(record.inputs),
            outputs=tuple(record.outputs),
            write_scope=tuple(record.write_scope),
            steps=tuple(record.steps),
            attempts=tuple(record.attempts),
            active_attempt=record.active_attempt,
            accepted_attempt=record.accepted_attempt,
        )


@dataclass(frozen=True)
class ProgressReport:
    task_id: str
    status: str
    accepted: int
    total: int
    pending: int
    verifying: int
    blocked: int
    integration: str
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id,
            "status": self.status,
            "accepted": self.accepted,
            "total": self.total,
            "pending": self.pending,
            "verifying": self.verifying,
            "blocked": self.blocked,
            "integration": self.integration,
            "next": self.next_action,
        }


@dataclass
class PlanRecord:
    """一个计划版本。

    ``members`` 是本轮纳入校验的全部任务；``leaves`` 是其中的叶子任务，
    用于展示“已验收叶子 / 纳入本轮叶子”。两者在大多数计划里相同。
    """

    id: str
    leaves: list[str] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    version: int = 1
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scope(self) -> list[str]:
        """本轮纳入校验的成员：显式 members 优先，否则退回到 leaves。"""
        return list(self.members) or list(self.leaves)


__all__ = [
    "ATTEMPT_STATUSES",
    "Artifact",
    "Attempt",
    "CREATE_COMMANDS",
    "EvidenceRef",
    "Event",
    "Kind",
    "PlanRecord",
    "ProgressReport",
    "Step",
    "TaskRecord",
    "TaskSnapshot",
]
