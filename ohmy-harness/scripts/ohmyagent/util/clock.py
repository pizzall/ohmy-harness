"""可注入的时钟；生产环境用 UTC，测试可以固定。"""

from __future__ import annotations

from datetime import datetime, timezone

_OVERRIDE: str | None = None


def now_utc() -> str:
    if _OVERRIDE is not None:
        return _OVERRIDE
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def freeze(value: str) -> None:
    global _OVERRIDE
    _OVERRIDE = value


def unfreeze() -> None:
    global _OVERRIDE
    _OVERRIDE = None
