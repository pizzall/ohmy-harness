"""迭代 1 的补充单元测试：写范围重叠、上限边界、scan 内容、CLI 新命令。"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner import parametrize, raises  # noqa: E402
from ohmyagent import ConflictError, Coordinator, TaskStatus  # noqa: E402
from ohmyagent.core.lifecycle import Limits  # noqa: E402
from ohmyagent.store import commands as C  # noqa: E402
from ohmyagent.store.commands import overlaps  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="oma-iter01u-"))


@parametrize(
    "pair",
    [
        (("src/a",), ("src/a",)),
        (("src/a",), ("src/a/b.py",)),
        (("src",), ("src/a/b.py",)),
        (("src/a/",), ("./src/a/b.py",)),
        (("src\\a",), ("src/a/b.py",)),
    ],
)
def test_overlaps_detects_nested_and_equal(pair):
    assert overlaps(*pair)


@parametrize(
    "pair",
    [
        (("src/a",), ("src/b",)),
        (("src/ab",), ("src/a",)),
        (("docs",), ("src",)),
        ((), ("src",)),
    ],
)
def test_overlaps_allows_disjoint(pair):
    assert overlaps(*pair) == []


def test_disjoint_scopes_run_in_parallel():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(
        C.CreateTask(task_id="x", intent="x", project_root=str(tmp), write_scope=("src/a",))
    )
    coord.execute(
        C.CreateTask(task_id="y", intent="y", project_root=str(tmp), write_scope=("src/b",))
    )
    coord.execute(C.DispatchTask(task_id="x", session="s-x"))
    coord.execute(C.DispatchTask(task_id="y", session="s-y"))
    assert coord.record("y").status is TaskStatus.IN_PROGRESS


def test_different_project_roots_do_not_clash():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(
        C.CreateTask(
            task_id="x", intent="x", project_root=str(tmp / "p1"), write_scope=("src/a",)
        )
    )
    coord.execute(
        C.CreateTask(
            task_id="y", intent="y", project_root=str(tmp / "p2"), write_scope=("src/a",)
        )
    )
    coord.execute(C.DispatchTask(task_id="x", session="s-x"))
    coord.execute(C.DispatchTask(task_id="y", session="s-y"))


def test_force_skips_write_scope_check():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(
        C.CreateTask(task_id="x", intent="x", project_root=str(tmp), write_scope=("src/a",))
    )
    coord.execute(
        C.CreateTask(task_id="y", intent="y", project_root=str(tmp), write_scope=("src/a",))
    )
    coord.execute(C.DispatchTask(task_id="x", session="s-x"))
    coord.execute(C.DispatchTask(task_id="y", session="s-y", force=True))
    assert coord.record("y").status is TaskStatus.IN_PROGRESS


def test_limits_are_configurable():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(
        C.DispatchTask(task_id="a", session="s", limits=Limits(max_attempts=1))
    )
    coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c"))
    coord.execute(C.Verify(task_id="a", outcome="rework", evidence="x"))
    with raises(ConflictError):
        coord.execute(
            C.BeginAttempt(task_id="a", session="s", limits=Limits(max_attempts=1))
        )


def test_scan_reports_session_and_workspace():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(
        C.DispatchTask(task_id="a", session="codex:abc", workspace=str(tmp / "wt-a"))
    )
    item = coord.scan()["dispatched_without_result"][0]
    assert item["session"] == "codex:abc"
    assert item["workspace"].endswith("wt-a")


def test_stale_submission_does_not_mutate_state():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s"))
    coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c1"))
    coord.execute(C.Verify(task_id="a", outcome="rework", evidence="x"))
    coord.execute(C.BeginAttempt(task_id="a", session="s"))
    before = coord.ledger.summarize().events
    event = coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c1"))
    assert event.kind == "attempt.late_result"
    assert coord.ledger.summarize().events == before + 1
    record = coord.record("a")
    assert record.status is TaskStatus.IN_PROGRESS
    assert record.attempt(1).status == "late"


def test_cli_dispatch_force_and_scan():
    tmp = _tmp()
    base = [sys.executable, "-m", "ohmyagent", "--root", str(tmp)]
    for args in (
        ["task", "create", "--id", "x", "--intent", "x", "--write-scope", "src/a"],
        ["task", "create", "--id", "y", "--intent", "y", "--write-scope", "src/a"],
        ["task", "dispatch", "--id", "x", "--session", "s"],
    ):
        proc = subprocess.run(base + args, cwd=str(ROOT), capture_output=True, encoding="utf-8")
        assert proc.returncode == 0, proc.stderr
    blocked = subprocess.run(
        base + ["task", "dispatch", "--id", "y", "--session", "s"],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
    )
    assert blocked.returncode == 1
    forced = subprocess.run(
        base + ["task", "dispatch", "--id", "y", "--session", "s", "--force"],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
    )
    assert forced.returncode == 0, forced.stderr
    scan = subprocess.run(
        base + ["scan"], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8"
    )
    assert scan.returncode == 0
    assert "dispatched_without_result" in scan.stdout
