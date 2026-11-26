"""受限视图。

两类读取方：
- 主控自己：看全部任务、尝试、证据；
- 正在执行的 Worker：只看自己这一项任务单，看不到其它任务的进度与验收结论。
"""

from __future__ import annotations

from typing import Any

from .core.errors import UnknownTaskError


def worker_view(coord: Any, task_id: str) -> dict[str, Any]:
    record = coord.record(task_id)
    if record is None:
        raise UnknownTaskError(task_id)
    attempt = record.attempt(record.active_attempt) if record.active_attempt else None
    return {
        "task": record.id,
        "intent": record.intent,
        "attempt": record.active_attempt,
        "session": attempt.session if attempt else "",
        "workspace": attempt.workspace if attempt else record.project_root,
        "inputs": list(record.inputs),
        "outputs": list(record.outputs),
        "write_scope": list(record.write_scope),
        "constraints": dict(record.constraints),
        "tasks": [
            {
                "task": record.id,
                "intent": record.intent,
                "attempt": record.active_attempt,
                "status": record.status.value,
            }
        ],
    }


__all__ = ["worker_view"]
