import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner import fixture  # noqa: E402
from ohmyagent import Coordinator, TaskStatus  # noqa: E402
from ohmyagent.store import commands as C  # noqa: E402


@fixture()
def tmp_path():
    import tempfile

    return Path(tempfile.mkdtemp(prefix="oma-progress-"))


def _plan(tmp: Path) -> Coordinator:
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="契约", project_root=str(tmp)))
    coord.execute(
        C.CreateTask(task_id="b", intent="服务端", project_root=str(tmp), deps=("a",))
    )
    coord.execute(
        C.CreateTask(task_id="c", intent="SDK", project_root=str(tmp), deps=("a",))
    )
    return coord


def _accept(coord: Coordinator, task_id: str) -> None:
    coord.execute(C.DispatchTask(task_id=task_id, session=f"s-{task_id}"))
    record = coord.record(task_id)
    coord.execute(
        C.SubmitResult(task_id=task_id, attempt=record.active_attempt, candidate="c")
    )
    coord.execute(C.Verify(task_id=task_id, outcome="pass", check="检查"))


def test_progress_counts_leaves_only(tmp_path):
    from ohmyagent.report.progress import project_progress

    coord = _plan(tmp_path)
    progress = project_progress(coord)
    assert progress["tasks"] == 3
    assert progress["leaves"] == 2
    assert progress["accepted"] == 0


def test_progress_uses_accepted_over_total(tmp_path):
    from ohmyagent.report.progress import project_progress

    coord = _plan(tmp_path)
    _accept(coord, "a")
    _accept(coord, "b")
    progress = project_progress(coord)
    assert progress["accepted"] == 1
    assert progress["leaves"] == 2


def test_task_progress_scope(tmp_path):
    from ohmyagent.report.progress import task_progress

    coord = _plan(tmp_path)
    report = task_progress(coord, "a")
    assert report["total"] == 2
    assert report["accepted"] == 0
    assert "派单" in report["next"]


def test_task_progress_next_is_verification_when_submitted(tmp_path):
    from ohmyagent.report.progress import task_progress

    coord = _plan(tmp_path)
    coord.execute(C.DispatchTask(task_id="b", session="s-b"))
    coord.execute(C.SubmitResult(task_id="b", attempt=1, candidate="c"))
    report = task_progress(coord, "a")
    assert report["next"] == "验收任务 b"
