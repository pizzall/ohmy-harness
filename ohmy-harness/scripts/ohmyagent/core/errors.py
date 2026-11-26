"""异常定义。所有面向主控的错误都是 OhMyAgentError 的子类，带稳定的 code。"""

from __future__ import annotations


class OhMyAgentError(Exception):
    code = "ohmyagent.error"


class UnknownTaskError(OhMyAgentError):
    code = "unknown_task"

    def __init__(self, task_id: str) -> None:
        super().__init__(f"找不到任务 {task_id}")
        self.task_id = task_id


class ConflictError(OhMyAgentError):
    """同一命令与当前状态冲突（例如对已取消的任务提交结果）。"""

    code = "conflict"


class OutOfScopeError(OhMyAgentError):
    """越过授权边界（例如任务被取消后仍要求 Worker 继续修改）。"""

    code = "out_of_scope"


class DuplicateTaskError(OhMyAgentError):
    code = "duplicate_task"
