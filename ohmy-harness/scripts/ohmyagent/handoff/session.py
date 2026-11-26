"""会话交接与回读验证。

- 写入：从任务记录生成一段结构化交接摘要（任务 ID、已验收版本、未完项、下一步、证据入口）；
- 读取：另一个会话显式读取这段摘要，能够说出上次交付、有效决定与下一步。
“写入成功”不等于闭环，只有新会话读回并核对通过才算闭环。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.lifecycle import TaskStatus

HANDOFF_JSON = "handoff.json"


@dataclass
class Handoff:
    task_id: str
    written_at: str
    accepted: list[dict[str, Any]] = field(default_factory=list)
    unfinished: list[dict[str, Any]] = field(default_factory=list)
    next: str = ""
    evidence: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task_id,
            "written_at": self.written_at,
            "accepted": self.accepted,
            "unfinished": self.unfinished,
            "next": self.next,
            "evidence": self.evidence,
            "decisions": self.decisions,
        }


def build(coord: Any, task_id: str, decisions_path: Path | None = None) -> Handoff:
    from ..util.clock import now_utc

    records = coord.records()
    record = records[task_id]
    related = {task_id} | _descendants(records, task_id)
    scope = [records[t] for t in related if t in records]
    accepted = [
        {
            "task": r.id,
            "attempt": r.accepted_attempt,
            "steps": [s.id for s in r.steps if s.outcome == "pass"],
            **({"review_scope": r.constraints["review_scope"]} if "review_scope" in r.constraints else {}),
        }
        for r in scope
        if r.status is TaskStatus.ACCEPTED
    ]
    unfinished = [
        {
            "task": r.id,
            "status": r.status.value,
            "attempts": r.number,
            **({"review_scope": r.constraints["review_scope"]} if "review_scope" in r.constraints else {}),
            **({"expected_outputs": r.outputs} if r.outputs else {}),
        }
        for r in scope
        if r.status is not TaskStatus.ACCEPTED
    ]
    handoff = Handoff(
        task_id=task_id,
        written_at=now_utc(),
        accepted=accepted,
        unfinished=unfinished,
        next=_next_actions(scope),
        evidence=[
            f"{r.id}: attempt {r.accepted_attempt}" for r in scope if r.accepted_attempt
        ],
        decisions=_decision_labels(decisions_path, record),
    )
    return handoff


def _next_actions(scope: list[Any]) -> str:
    from ..report.progress import next_action

    if all(r.status is TaskStatus.ACCEPTED for r in scope):
        return "全部已验收，可以集成交付"
    return next_action(scope)


def _decision_labels(path: Path | None, record: Any) -> list[str]:
    if path is None:
        return []
    from ..memory.decisions import Decisions

    scope = record.constraints.get("decision_scope") or record.id
    return [d.statement for d in Decisions(path).applicable(scope)]


def _descendants(records: dict[str, Any], task_id: str) -> set[str]:
    from ..report.progress import descendants

    return descendants(records, task_id)


def save(handoff: Handoff, state: Path) -> Path:
    path = Path(state) / HANDOFF_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(handoff.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load(state: Path) -> Handoff:
    path = Path(state) / HANDOFF_JSON
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Handoff(
        task_id=payload["task"],
        written_at=payload["written_at"],
        accepted=payload.get("accepted", []),
        unfinished=payload.get("unfinished", []),
        next=payload.get("next", ""),
        evidence=payload.get("evidence", []),
        decisions=payload.get("decisions", []),
    )


def verify(handoff: Handoff, coord: Any) -> list[str]:
    """在一个新会话里核对交接内容是否仍与记录一致，返回发现的问题。"""
    problems: list[str] = []
    records = coord.records()
    related = {handoff.task_id} | _descendants(records, handoff.task_id)
    items = {item["task"]: item for item in [*handoff.accepted, *handoff.unfinished]}
    for task in related:
        record = records.get(task)
        scope = record.constraints.get("review_scope") if record else None
        if scope and items.get(task, {}).get("review_scope") != scope:
            problems.append(f"任务 {task} 的交接遗漏或改变了冻结的审查范围")
    for item in handoff.accepted:
        record = records.get(item["task"])
        if record is None:
            problems.append(f"交接中的任务 {item['task']} 已不存在")
            continue
        if record.accepted_attempt != item["attempt"]:
            problems.append(
                f"任务 {item['task']} 已验收版本变了："
                f"交接 {item['attempt']}，实际 {record.accepted_attempt}"
            )
    for item in handoff.unfinished:
        record = records.get(item["task"])
        if record is None:
            problems.append(f"交接中的任务 {item['task']} 已不存在")
            continue
        if record.status.value != item["status"]:
            problems.append(
                f"任务 {item['task']} 状态变了：交接 {item['status']}，"
                f"实际 {record.status.value}"
            )
    return problems


__all__ = ["Handoff", "build", "load", "save", "verify"]
