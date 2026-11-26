"""登记本项目实际使用的账本审查入口，探针只使用临时原件和内存任务。"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ..core.errors import ConflictError
from ..core.lifecycle import TaskStatus
from ..store import commands as C
from ..store.record import Attempt, Event, Kind, TaskRecord
from ..store.review import file_hash

RECEIPT = "review-hook.json"
HOOK = "review-scope"
ENTRY = str(Path(C.__file__).resolve())


def _probe_review(work: Path) -> None:
    source = "original.txt"
    (work / source).write_text("first\nsecond\n", encoding="utf-8")
    digest = file_hash(str(work), source)
    items = {key: {"source": source, "method": "read", "criterion": key} for key in ("first", "second")}
    attempt = Attempt(1, "init", str(work), status="submitted", candidate="v1", review_versions={source: digest})
    record = TaskRecord("probe", TaskStatus.VERIFYING, role="review", attempts=[attempt], active_attempt=1,
                        constraints={"review_scope": {"items": items, "excluded": []}})
    proofs = [{"id": key, "source": source, "sha256": digest, "method": "read", "finding": key, "outcome": "pass"} for key in items]

    def verify(rows: list[dict]) -> Event:
        batch = {"task": record.id, "attempt": 1, "candidate": "v1", "items": rows}
        return C.run({record.id: record}, C.Verify(record.id, "pass", review_evidence=(batch,)), 1, None).event

    try:
        verify(proofs[:1])
    except ConflictError:
        pass
    else:
        raise ConflictError("审查入口放行了缺项证据")
    event = verify(proofs)
    if event.kind != Kind.VERIFIED or event.data.get("review_items") != proofs:
        raise ConflictError("审查入口未按完整证据登记通过")


def register_review_hook(state: Path) -> dict[str, str]:
    """调用方持有项目账本锁。成功和失败均原子替换上次冷启动回执。"""
    receipt = {"hook": HOOK, "entry": ENTRY, "status": "ready"}
    with TemporaryDirectory(prefix="init-", dir=state) as directory:
        work = Path(directory)
        try:
            _probe_review(work)
        except Exception as exc:
            receipt.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        staged = work / RECEIPT
        staged.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        staged.replace(state / RECEIPT)
    return receipt


def require_review_hook(state: Path) -> None:
    path = state / RECEIPT
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # 老项目首次放行时也走同一注册入口。
        receipt = register_review_hook(state)
    except (ValueError, OSError) as exc:
        raise ConflictError("无法读取审查 hook 回执，请重新运行 oma init") from exc
    if not isinstance(receipt, dict) or (receipt.get("hook"), receipt.get("entry"), receipt.get("status")) != (HOOK, ENTRY, "ready"):
        raise ConflictError("本项目审查 hook 尚未就绪，请用当前账本入口重新运行 oma init")
