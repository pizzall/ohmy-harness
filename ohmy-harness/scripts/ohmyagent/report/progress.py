"""进度统计。

同一计划版本内展示“已验收的叶子任务数 / 纳入本轮的叶子任务数”，阻塞单列；
心跳只证明存活，不算实质进展。
"""

from __future__ import annotations

from typing import Any

from ..core.lifecycle import TaskStatus
from ..store.record import ProgressReport, TaskRecord


def children_index(records: dict[str, TaskRecord]) -> dict[str, set[str]]:
    children: dict[str, set[str]] = {}
    for record in records.values():
        for dep in record.deps:
            children.setdefault(dep, set()).add(record.id)
    return children


def leaves(records: dict[str, TaskRecord]) -> list[TaskRecord]:
    """叶子任务：没有任何其它任务依赖它的任务。"""
    children = children_index(records)
    return [r for r in records.values() if not children.get(r.id)]


def descendants(records: dict[str, TaskRecord], task_id: str) -> set[str]:
    children = children_index(records)
    found: set[str] = set()
    stack = list(children.get(task_id, ()))
    while stack:
        current = stack.pop()
        if current in found or current == task_id:
            continue
        found.add(current)
        stack.extend(children.get(current, ()))
    return found


def scope_leaves(records: dict[str, TaskRecord], task_id: str) -> list[TaskRecord]:
    related = descendants(records, task_id) | {task_id}
    scope = {tid: records[tid] for tid in related if tid in records}
    result = [r for r in scope.values() if not (children_index(scope).get(r.id))]
    return result or ([records[task_id]] if task_id in records else [])


def next_action(scope: list[TaskRecord]) -> str:
    ranked = sorted(scope, key=lambda r: (STATUS_ORDER.index(r.status), r.id))
    for record in ranked:
        if record.status is TaskStatus.VERIFYING:
            return f"验收任务 {record.id}"
        if record.status is TaskStatus.IN_PROGRESS:
            return f"等待任务 {record.id} 的结果"
        if record.status is TaskStatus.PENDING:
            return f"派单任务 {record.id}"
        if record.status is TaskStatus.BLOCKED:
            return f"处理任务 {record.id} 的阻塞"
    return "集成验收"


STATUS_ORDER = [
    TaskStatus.VERIFYING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.PENDING,
    TaskStatus.BLOCKED,
    TaskStatus.ACCEPTED,
    TaskStatus.CANCELLED,
]


def task_progress(coord: Any, task_id: str) -> dict[str, Any]:
    records = coord.records()
    record = records.get(task_id)
    if record is None:
        raise KeyError(task_id)
    scope = scope_leaves(records, task_id)
    accepted = sum(1 for r in scope if r.status is TaskStatus.ACCEPTED)
    pending = sum(1 for r in scope if r.status is TaskStatus.PENDING)
    verifying = sum(1 for r in scope if r.status is TaskStatus.VERIFYING)
    blocked = sum(1 for r in scope if r.status is TaskStatus.BLOCKED)
    report = ProgressReport(
        task_id=task_id,
        status=record.status.value,
        accepted=accepted,
        total=len(scope),
        pending=pending,
        verifying=verifying,
        blocked=blocked,
        integration="not_started",
        next_action=next_action(scope),
    )
    return report.to_dict()


def project_progress(coord: Any, plan: str | None = None) -> dict[str, Any]:
    records = coord.records()
    if plan:
        plan_record = records.get(plan)
        if plan_record is None or plan_record.plan is None:
            raise KeyError(plan)
        planned_order = list(plan_record.plan.scope)
        members = [tid for tid in planned_order if tid in records]
        # First pass: which planned members are depended on by other planned members.
        depended = set()
        for tid in planned_order:
            rec = records.get(tid)
            if rec is None:
                continue
            for dep in rec.deps:
                if dep != tid:
                    for other in planned_order:
                        if other == dep:
                            depended.add(other)
                            break
        # Second pass: the rest are this round's leaves (progress denominator).
        leaves_in_plan = [tid for tid in planned_order if tid in records and tid not in depended]
        if not leaves_in_plan:
            leaves_in_plan = [tid for tid in planned_order if tid in records]
        scoped = [records[tid] for tid in leaves_in_plan]
        accepted = [x for x in scoped if x.status is TaskStatus.ACCEPTED]
        history = plan_record.plan.history[-1] if plan_record.plan.history else {}
        return {
            "plan": plan_record.plan.id,
            "version": plan_record.plan.version,
            "total": len(leaves_in_plan),
            "accepted": len(accepted),
            "members": len(members),
            "pending": len([x for x in scoped if x.status is TaskStatus.PENDING]),
            "in_progress": len(
                [x for x in scoped if x.status is TaskStatus.IN_PROGRESS]
            ),
            "verifying": len([x for x in scoped if x.status is TaskStatus.VERIFYING]),
            "blocked": len([x for x in scoped if x.status is TaskStatus.BLOCKED]),
            "cancelled": len([x for x in scoped if x.status is TaskStatus.CANCELLED]),
            "missing": [tid for tid in planned_order if tid not in records],
            "differences": {
                "added": list(history.get("added") or []),
                "removed": list(history.get("removed") or []),
            },
            "next": next_action(scoped) if scoped else "派单任务",
        }
    leafs = leaves(records)
    return {
        "tasks": len(records),
        "leaves": len(leafs),
        "accepted": sum(1 for r in leafs if r.status is TaskStatus.ACCEPTED),
        "pending": sum(1 for r in leafs if r.status is TaskStatus.PENDING),
        "in_progress": sum(1 for r in leafs if r.status is TaskStatus.IN_PROGRESS),
        "verifying": sum(1 for r in leafs if r.status is TaskStatus.VERIFYING),
        "blocked": sum(1 for r in leafs if r.status is TaskStatus.BLOCKED),
        "cancelled": sum(1 for r in leafs if r.status is TaskStatus.CANCELLED),
        "per_task": {r.id: r.status.value for r in sorted(records.values(), key=lambda r: r.id)},
    }


__all__ = [
    "children_index",
    "descendants",
    "leaves",
    "next_action",
    "project_progress",
    "scope_leaves",
    "task_progress",
]
