"""ohmy-harness 的单宿主任务账本。

只依赖 Python 3 标准库。对外 API 以本包的顶层名字为准，内部模块可以调整。
"""

from .core.errors import (
    ConflictError,
    DuplicateTaskError,
    OhMyAgentError,
    UnknownTaskError,
)
from .core.lifecycle import LandingOutcome, TaskStatus
from .coord import Coordinator, CorruptionError, file_lock, replay
from .store.record import (
    Artifact,
    Attempt,
    Event,
    Kind,
    ProgressReport,
    Step,
    TaskRecord,
    TaskSnapshot,
)

__version__ = "2.0.0"

__all__ = [
    "Artifact",
    "Attempt",
    "ConflictError",
    "Coordinator",
    "CorruptionError",
    "DuplicateTaskError",
    "Event",
    "Kind",
    "LandingOutcome",
    "OhMyAgentError",
    "ProgressReport",
    "Step",
    "TaskRecord",
    "TaskSnapshot",
    "TaskStatus",
    "UnknownTaskError",
    "file_lock",
    "replay",
    "__version__",
]
