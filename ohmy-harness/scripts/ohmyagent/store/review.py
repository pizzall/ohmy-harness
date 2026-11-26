"""审查范围在创建时固定。放行前核对逐项结论、来源和实读版本。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..core.errors import ConflictError
from .record import Attempt, TaskRecord


REVIEW_ROLES = {"review", "reviewer", "audit", "auditor", "qa", "审查", "审核", "终审", "审阅", "验收"}


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_scope(scope: Any) -> None:
    if not isinstance(scope, dict) or set(scope) != {"items", "excluded"}:
        raise ConflictError("审查范围需要 items 和 excluded")
    if not isinstance(scope["excluded"], list) or not all(_text(item) for item in scope["excluded"]):
        raise ConflictError("excluded 应列出明确的范围外事项，可为空数组")
    items = scope["items"]
    if not isinstance(items, dict) or not items:
        raise ConflictError("审查范围至少需要一个验收项")
    for key, unit in items.items():
        fields = {"source", "method", "criterion"}
        if not _text(key) or not isinstance(unit, dict) or set(unit) != fields or not all(map(_text, unit.values())):
            raise ConflictError(f"审查项 {key} 需要来源文件、核验方法和成功条件")


def file_hash(workspace: str, source: str) -> str:
    path = Path(workspace) / source
    try:
        if not path.is_file():
            raise ConflictError(f"找不到可核对的文件 {path}")
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise ConflictError(f"无法核对文件 {path}：{exc}") from exc


def review_versions(record: TaskRecord, attempt: Attempt) -> dict[str, str]:
    scope = record.constraints.get("review_scope")
    if not scope:
        return {}
    sources = dict.fromkeys(item["source"] for item in scope["items"].values())
    return {source: file_hash(attempt.workspace, source) for source in sources}


def check_coverage(record: TaskRecord, attempt: Attempt, batches: tuple[dict, ...]) -> list[dict]:
    scope = record.constraints["review_scope"]["items"]
    checked = {}
    for batch in batches:
        if not isinstance(batch, dict) or (batch.get("task"), batch.get("attempt"), batch.get("candidate")) != (
            record.id, attempt.number, attempt.candidate
        ):
            raise ConflictError("审查证据与当前任务、尝试号或候选版本不符")
        if not isinstance(batch.get("items"), list):
            raise ConflictError("证据批次缺少 items 数组")
        for proof in batch["items"]:
            key = proof.get("id") if isinstance(proof, dict) else None
            if not _text(key) or key not in scope or key in checked:
                raise ConflictError(f"审查证据含范围外或重复的项 {key}")
            unit = scope[key]
            if proof.get("source") != unit["source"] or proof.get("method") != unit["method"] or not _text(proof.get("finding")):
                raise ConflictError(f"审查项 {key} 需要按原定来源和方法给出具体结论")
            if proof.get("outcome") != "pass":
                raise ConflictError(f"审查项 {key} 尚未通过")
            if not proof.get("sha256") or proof["sha256"] != attempt.review_versions.get(unit["source"]):
                raise ConflictError(f"审查项 {key} 未使用送验版本")
            checked[key] = proof
    missing = scope.keys() - checked.keys()
    if missing:
        raise ConflictError(f"仍待审查：{', '.join(sorted(missing))}")
    if review_versions(record, attempt) != attempt.review_versions:
        raise ConflictError("送验后来源文件已改变，请提交新候选并复核受影响的项")
    return [checked[key] for key in scope]
