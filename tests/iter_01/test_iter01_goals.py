"""迭代 1 的目标测试：返工上限、并发写冲突、主控重启核对、陈旧结果。

这些用例先在本迭代开始时的代码上跑（预期失败），改造后再跑（预期全绿）。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner import raises  # noqa: E402
from ohmyagent import (  # noqa: E402
    ConflictError,
    Coordinator,
    DuplicateTaskError,
    TaskStatus,
)
from ohmyagent.store import commands as C  # noqa: E402


def _tmp() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="oma-iter01-"))


def _accept(coord: Coordinator, task_id: str) -> None:
    record = coord.record(task_id)
    coord.execute(
        C.SubmitResult(task_id=task_id, attempt=record.active_attempt, candidate="c")
    )
    coord.execute(C.Verify(task_id=task_id, outcome="pass"))


def _rework(coord: Coordinator, task_id: str) -> None:
    record = coord.record(task_id)
    coord.execute(C.SubmitResult(task_id=task_id, attempt=record.active_attempt, candidate="c"))
    coord.execute(C.Verify(task_id=task_id, outcome="rework", evidence="x"))
    coord.execute(C.BeginAttempt(task_id=task_id, session=f"s-{task_id}"))


# ---- 目标 1：业务返工上限（首次 + 最多两次返工 = 最多 3 次尝试） ------------------


def test_rework_limit_allows_three_attempts():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s"))
    _rework(coord, "a")
    _rework(coord, "a")
    assert coord.record("a").number == 3


def test_rework_limit_rejects_fourth_attempt():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s"))
    _rework(coord, "a")
    _rework(coord, "a")
    record = coord.record("a")
    coord.execute(C.SubmitResult(task_id="a", attempt=record.active_attempt, candidate="c"))
    coord.execute(C.Verify(task_id="a", outcome="rework", evidence="x"))
    with raises(ConflictError):
        coord.execute(C.BeginAttempt(task_id="a", session="s"))


def test_successful_attempt_does_not_consume_rework():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s"))
    _accept(coord, "a")
    assert coord.record("a").number == 1


# ---- 目标 2：并发写冲突 ------------------------------------------------------


def test_concurrent_write_on_same_scope_is_rejected():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(
        C.CreateTask(task_id="x", intent="x", project_root=str(tmp), write_scope=("src/a",))
    )
    coord.execute(
        C.CreateTask(task_id="y", intent="y", project_root=str(tmp), write_scope=("src/a",))
    )
    coord.execute(C.DispatchTask(task_id="x", session="s-x"))
    with raises(ConflictError):
        coord.execute(C.DispatchTask(task_id="y", session="s-y"))


def test_concurrent_write_after_waiting_done_is_allowed():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(
        C.CreateTask(task_id="x", intent="x", project_root=str(tmp), write_scope=("src/a",))
    )
    coord.execute(
        C.CreateTask(task_id="y", intent="y", project_root=str(tmp), write_scope=("src/a",))
    )
    coord.execute(C.DispatchTask(task_id="x", session="s-x"))
    coord.execute(C.SubmitResult(task_id="x", attempt=1, candidate="c"))
    coord.execute(C.Verify(task_id="x", outcome="pass"))
    coord.execute(C.DispatchTask(task_id="y", session="s-y"))
    assert coord.record("y").status is TaskStatus.IN_PROGRESS


# ---- 目标 3：主控重启后的“已派出但无结果”核对 --------------------------------


def test_scan_detects_dispatched_without_result():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s"))
    fresh = Coordinator(tmp / ".superagent")
    report = fresh.scan()
    assert [item["task"] for item in report["dispatched_without_result"]] == ["a"]


def test_scan_after_result_is_clean():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s"))
    coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c"))
    fresh = Coordinator(tmp / ".superagent")
    assert fresh.scan()["dispatched_without_result"] == []


# ---- 目标 4：陈旧结果保护 ----------------------------------------------------


def test_stale_submission_of_old_attempt_is_rejected():
    # 迭代 3 起：旧尝试的迟到结果不再改变状态，而是留档为 late（方案 §4 迟到结果）
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s"))
    coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c1"))
    coord.execute(C.Verify(task_id="a", outcome="rework", evidence="x"))
    coord.execute(C.BeginAttempt(task_id="a", session="s"))
    event = coord.execute(
        C.SubmitResult(task_id="a", attempt=1, candidate="c1-again")
    )
    assert event.kind == "attempt.late_result"
    record = coord.record("a")
    assert record.attempt(1).status == "late"
    assert record.status is TaskStatus.IN_PROGRESS
    assert record.active_attempt == 2


def test_stale_verification_of_old_attempt_is_rejected():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s"))
    coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c1"))
    coord.execute(C.Verify(task_id="a", outcome="rework", evidence="x"))
    coord.execute(C.BeginAttempt(task_id="a", session="s"))
    coord.execute(C.SubmitResult(task_id="a", attempt=2, candidate="c2"))
    with raises(ConflictError):
        coord.execute(C.Verify(task_id="a", outcome="pass", attempt=1))


# ---- 目标 5：离线执行判定 ----------------------------------------------------


def test_capability_report_marks_missing_executors():
    from unittest.mock import patch

    tmp = _tmp()
    from ohmyagent.capability import probe

    with patch("ohmyagent.capability.shutil.which", return_value=None):
        report = probe(tmp, home=tmp)
    statuses = {item["executor"]: item for item in report["executors"]}
    assert "mock" in statuses
    assert statuses["mock"]["status"] == "offline"
    assert report["allow"]["allowed"] == []
    assert set(report["allow"]["unavailable"]) == {"codex", "cursor", "gemini"}
