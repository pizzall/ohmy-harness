"""任务生命周期。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"  # 待调度：已登记，还没派给 Worker
    IN_PROGRESS = "in_progress"  # 执行中：Worker 正在做
    VERIFYING = "verifying"  # 待验证：Worker 已提交候选产物
    ACCEPTED = "accepted"  # 子任务已验收
    BLOCKED = "blocked"  # 阻塞：环境/授权问题，等主控处理
    CANCELLED = "cancelled"  # 已取消：迟到结果不再改变状态

    @property
    def is_active(self) -> bool:
        return self in ACTIVE

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL


ACTIVE = frozenset({TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.VERIFYING})
ACCEPTED_STATUSES = frozenset({TaskStatus.ACCEPTED})
TERMINAL = frozenset({TaskStatus.CANCELLED})

# 主控视角“还欠工作”的状态：阻塞和取消也计入未完成，但计数单列。
UNFINISHED = ACTIVE | frozenset({TaskStatus.BLOCKED})

# 可以动的状态（派单/断言/返工/交接）。终态只接受登记记录类命令。
MUTABLE = ACTIVE | frozenset({TaskStatus.BLOCKED, TaskStatus.ACCEPTED})

# 一个尝试（attempt）的状态。
ATTEMPT_STATUSES = ("running", "awaiting_reconciliation", "submitted", "abandoned")


class ActiveStatuses:
    """按语义细分的状态集合，避免调用方各自拼 frozenset。"""

    __slots__ = ()

    active = ACTIVE
    unfinished = UNFINISHED
    accepted = ACCEPTED_STATUSES
    terminal = TERMINAL
    mutable = MUTABLE


class LandingOutcome(str, Enum):
    ACCEPTED = "accepted"
    REWORK = "rework"
    BLOCKED = "blocked"

    def __str__(self) -> str:  # 便于日志与 JSON
        return self.value


@dataclass(frozen=True)
class Limits:
    """一局任务的可调上限。"""

    max_attempts: int = 3
    max_timeout: float | None = None


DEFAULT_LIMITS = Limits()


def can_dispatch(status: TaskStatus) -> bool:
    return status is TaskStatus.PENDING


def can_attempt(status: TaskStatus) -> bool:
    """可以开始新尝试（返工）的状态：待调度或阻塞。"""
    return status in (TaskStatus.PENDING, TaskStatus.BLOCKED)


def can_verify(status: TaskStatus) -> bool:
    return status is TaskStatus.VERIFYING


def can_integrate(accepted_leaves: int, total_leaves: int) -> bool:
    """集成验收只在所有纳入本轮的叶子任务都已验收时允许。"""
    return total_leaves > 0 and accepted_leaves == total_leaves


def can_commit(status: TaskStatus) -> bool:
    return status is TaskStatus.ACCEPTED


__all__ = [
    "ACCEPTED_STATUSES",
    "ACTIVE",
    "ActiveStatuses",
    "ATTEMPT_STATUSES",
    "DEFAULT_LIMITS",
    "LandingOutcome",
    "Limits",
    "MUTABLE",
    "TaskStatus",
    "TERMINAL",
    "UNFINISHED",
    "can_attempt",
    "can_commit",
    "can_dispatch",
    "can_integrate",
    "can_verify",
]
