"""集成验收与落地。

规则：
1. 下游默认引用“已验收版本”，不引用草稿；
2. 主控按依赖顺序集成，在组合版本上复跑跨模块检查；
3. 依赖变化只重验受影响的部分；
4. 验收撤回（landing=rework）只影响依赖它的下游。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.errors import ConflictError, UnknownTaskError
from ..core.lifecycle import LandingOutcome, TaskStatus
from ..store import commands as C
from ..store.record import TaskRecord

OUTCOMES = {o.value for o in LandingOutcome}


@dataclass(frozen=True)
class Order:
    ready: list[str]
    waiting: list[str]


def unaccepted_deps(records: dict[str, TaskRecord], task_id: str) -> list[str]:
    record = records[task_id]
    return [
        d
        for d in record.deps
        if d in records and records[d].status is not TaskStatus.ACCEPTED
    ]


def order_landing(records: dict[str, TaskRecord], task_ids: list[str]) -> Order:
    """按依赖顺序排列落地计划：只有“已验收且依赖都已验收”的任务算就绪。

    就绪顺序由依赖关系决定（被依赖的先落地），与输入列表顺序无关。
    """
    in_scope = [tid for tid in task_ids if tid in records]
    accepted = {
        tid for tid in in_scope if records[tid].status is TaskStatus.ACCEPTED
    }
    depth: dict[str, int] = {}

    def _depth(tid: str, seen: frozenset[str] = frozenset()) -> int:
        if tid in depth:
            return depth[tid]
        if tid in seen:
            return 0
        deps = _deps_in(records, tid, task_ids)
        value = 0 if not deps else 1 + max(_depth(d, seen | {tid}) for d in deps)
        depth[tid] = value
        return value

    for tid in in_scope:
        _depth(tid)
    ready = sorted(
        (tid for tid in in_scope if tid in accepted),
        key=lambda tid: (depth[tid], tid),
    )
    waiting = [tid for tid in task_ids if tid not in records]
    waiting.extend(tid for tid in in_scope if tid not in accepted)
    return Order(ready=ready, waiting=waiting)


def _deps_in(records: dict[str, TaskRecord], task_id: str, task_ids: list[str]) -> list[str]:
    record = records[task_id]
    return [d for d in record.deps if d in task_ids]


def check_landable(records: dict[str, TaskRecord], task_id: str) -> list[str]:
    """落地前检查：返回阻塞落地的依赖。自身已验收或依赖都已验收才放行。"""
    if records[task_id].status is not TaskStatus.ACCEPTED:
        return [f"{task_id} 自身尚未验收"]
    return unaccepted_deps(records, task_id)


def land(coord: Any, task_id: str, outcome: str = "accepted", reason: str = "") -> Any:
    if outcome not in OUTCOMES:
        raise ConflictError(f"未知落地结论 {outcome}")
    records = coord.records()
    if task_id not in records:
        raise UnknownTaskError(task_id)
    if outcome == "accepted":
        blockers = check_landable(records, task_id)
        if blockers:
            raise ConflictError(
                f"任务 {task_id} 不能落地：{', '.join(blockers)}"
            )
    return coord.execute(C.DecideLanding(task_id=task_id, outcome=outcome, reason=reason))


def reopen(coord: Any, task_id: str, reason: str = "") -> list[Any]:
    """验收撤回：任务回到待验证，依赖它的已验收下游一并撤回。"""
    records = coord.records()
    if task_id not in records:
        raise UnknownTaskError(task_id)
    events = []
    affected: set[str] = set()
    stack = [task_id]
    from ..report.progress import children_index

    children = children_index(records)
    while stack:
        current = stack.pop()
        if current in affected:
            continue
        affected.add(current)
        stack.extend(children.get(current, ()))
    for tid in sorted(affected):
        record = records[tid]
        if tid == task_id and record.status is TaskStatus.ACCEPTED:
            events.append(
                coord.execute(C.DecideBlock(task_id=tid, kind="reopen", reason=reason))
            )
        elif record.status is TaskStatus.ACCEPTED:
            events.append(
                coord.execute(C.DecideBlock(task_id=tid, kind="dependency_changed", reason=reason))
            )
    return events


__all__ = [
    "Order",
    "check_landable",
    "land",
    "order_landing",
    "reopen",
    "unaccepted_deps",
]
