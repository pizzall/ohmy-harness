"""共享记忆。

每条决定记录：适用范围、来源（任务/尝试/用户）、证据、确认时间、状态与替代关系。
Worker 提交候选，主控确认；被替代的旧决定保留但不再被读取。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..util.clock import now_utc

DECISIONS = "decisions.json"


@dataclass
class Decision:
    id: str
    scope: str
    statement: str
    source: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    confirmed_at: str = ""
    status: str = "candidate"  # candidate / confirmed / superseded
    supersedes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Decisions:
    def __init__(self, state: Path) -> None:
        self.path = Path(state) / DECISIONS

    def load(self) -> list[Decision]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [Decision(**item) for item in raw]

    def save(self, decisions: list[Decision]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            json.dumps(d.to_dict(), ensure_ascii=False, sort_keys=True)
            for d in decisions
        ]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("[" + ",\n".join(payload) + "]\n", encoding="utf-8")
        tmp.replace(self.path)

    def propose(
        self,
        *,
        id: str,
        scope: str,
        statement: str,
        source: dict[str, Any] | None = None,
        evidence: list[str] | None = None,
    ) -> Decision:
        decision = Decision(
            id=id,
            scope=scope,
            statement=statement,
            source=dict(source or {}),
            evidence=list(evidence or []),
            status="candidate",
        )
        items = self.load()
        items.append(decision)
        self.save(items)
        return decision

    def confirm(self, id: str, *, supersedes: list[str] | None = None) -> Decision:
        items = self.load()
        found = None
        replaced = supersedes or []
        for item in items:
            if item.id in replaced and item.status == "confirmed":
                item.status = "superseded"
                text = item.statement
                if id not in item.supersedes:
                    item.supersedes.append(id)
                _ = text
            if item.id == id:
                item.status = "confirmed"
                item.confirmed_at = now_utc()
                if supersedes:
                    item.supersedes = list(supersedes)
                found = item
        if found is None:
            raise KeyError(id)
        self.save(items)
        return found

    def applicable(self, scope: str) -> list[Decision]:
        """读取可用的决定：状态为 confirmed，且范围匹配（相同或为父范围）。"""
        items = self.load()
        result = []
        for item in items:
            if item.status != "confirmed":
                continue
            if item.scope == scope or scope.startswith(item.scope + "/") or item.scope == "*":
                result.append(item)
        return result


__all__ = ["Decision", "Decisions"]
