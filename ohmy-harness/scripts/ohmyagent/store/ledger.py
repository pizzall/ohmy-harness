"""事件日志的读写。

- 写：追加一行 JSON，`fsync` 落盘；序号连续，`prev` 指向上一行哈希。
- 读：容忍最后一行被截断（进程写一半时崩溃），回放前面所有完整行。
- 损坏：中间行无法解析时报 :class:`CorruptLedgerError`，绝不猜测内容。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .record import Event

RECORD = "record.txt"
LOCK = "lock"
RUN = "run"


class CorruptLedgerError(Exception):
    """日志在中途出现无法跳过的损坏（不是最后一行的截断）。"""


def stable_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()


@dataclass
class Summary:
    seq: int = 0
    digest: str = ""
    tasks: int = 0
    events: int = 0


class Ledger:
    """在 `state` 目录下维护 record.txt / lock / run。"""

    def __init__(self, state: Path) -> None:
        self.state = Path(state)
        self.record = self.state / RECORD
        self.lock = self.state / LOCK
        self.run = self.state / RUN

    def ensure(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)

    # ---- 读 -------------------------------------------------------------------

    def read(self) -> list[Event]:
        if not self.record.exists():
            return []
        text = self.record.read_bytes().decode("utf-8", errors="replace")
        lines = [line.strip() for line in text.split("\n")]
        meaningful = [i for i, line in enumerate(lines) if line]
        last = meaningful[-1] if meaningful else -1
        events: list[Event] = []
        for index, line in enumerate(lines):
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                if index == last:
                    break  # 最后一行写了一半：忽略
                raise CorruptLedgerError(
                    f"{self.record} 第 {index + 1} 行无法解析"
                ) from None
            events.append(
                Event(
                    seq=int(payload["seq"]),
                    kind=str(payload["kind"]),
                    task_id=str(payload["task"]),
                    data=dict(payload.get("data") or {}),
                    prev=payload.get("prev"),
                )
            )
        return events

    def last_hash(self, events: list[Event]) -> str | None:
        if not events:
            return None
        return stable_hash(events[-1].to_line())

    def summarize(self) -> Summary:
        events = self.read()
        return Summary(
            seq=events[-1].seq if events else 0,
            digest=self.last_hash(events) or "",
            tasks=len({e.task_id for e in events}),
            events=len(events),
        )

    # ---- 写 -------------------------------------------------------------------

    def append(self, events: list[Event]) -> None:
        if not events:
            return
        self.ensure()
        payload = "".join(e.to_line() + "\n" for e in events).encode("utf-8")
        fd = os.open(self.record, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def repair_tail(self) -> bool:
        """如果最后一行是不完整的一行，把它截掉。返回是否做了修改。"""
        if not self.record.exists():
            return False
        raw = self.record.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return False
        cut = raw.rfind(b"\n")
        self.record.write_bytes(raw[: cut + 1] if cut >= 0 else b"")
        return True

    # ---- 运行标记 -------------------------------------------------------------

    def mark_run(self, run_id: str) -> None:
        self.ensure()
        self.run.write_text(run_id, encoding="utf-8")

    def running(self) -> str | None:
        if self.run.exists():
            text = self.run.read_text(encoding="utf-8").strip()
            return text or None
        return None

    def clear_run(self) -> None:
        if self.run.exists():
            self.run.unlink()


@dataclass
class Notebook:
    """调试用：事件序号与主键。"""

    entries: list[tuple[int, str]] = field(default_factory=list)


__all__ = [
    "CorruptLedgerError",
    "Ledger",
    "LOCK",
    "Notebook",
    "RECORD",
    "RUN",
    "Summary",
    "stable_hash",
]
