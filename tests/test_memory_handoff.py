import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner import raises  # noqa: E402
from ohmyagent import ConflictError, Coordinator, TaskStatus  # noqa: E402
from ohmyagent.handoff import build, load, save, verify  # noqa: E402
from ohmyagent.memory import Decisions  # noqa: E402
from ohmyagent.store import commands as C  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="oma-memory-"))


def _accept(coord: Coordinator, task_id: str) -> None:
    coord.execute(C.DispatchTask(task_id=task_id, session=f"s-{task_id}"))
    coord.execute(C.SubmitResult(task_id=task_id, attempt=1, candidate="c"))
    coord.execute(C.Verify(task_id=task_id, outcome="pass", check="通过"))


def _plan(tmp: Path) -> Coordinator:
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="契约", project_root=str(tmp)))
    coord.execute(
        C.CreateTask(task_id="b", intent="服务端", project_root=str(tmp), deps=("a",))
    )
    return coord


def test_decisions_are_candidates_by_default():
    store = Decisions(Path(tempfile.mkdtemp()))
    store.propose(id="d-old-client", scope="orders", statement="老客户端首次请求无游标")
    assert store.applicable("orders") == []


def test_confirmed_decision_applies_to_scope():
    store = Decisions(Path(tempfile.mkdtemp()))
    store.propose(id="d1", scope="orders", statement="老客户端首次请求无游标")
    store.confirm("d1")
    assert [d.id for d in store.applicable("orders")] == ["d1"]
    assert [d.id for d in store.applicable("orders/paging")] == ["d1"]
    assert store.applicable("other") == []


def test_superseding_decision_replaces_old():
    store = Decisions(Path(tempfile.mkdtemp()))
    store.propose(id="d1", scope="orders", statement="旧约定")
    store.confirm("d1")
    store.propose(id="d2", scope="orders", statement="新约定")
    store.confirm("d2", supersedes=["d1"])
    assert [d.id for d in store.applicable("orders")] == ["d2"]


def test_handoff_roundtrip():
    tmp = _tmp()
    coord = _plan(tmp)
    _accept(coord, "a")
    handoff = build(coord, "a")
    assert handoff.accepted and handoff.unfinished
    assert handoff.next == "派单任务 b"
    save(handoff, tmp / ".superagent")
    loaded = load(tmp / ".superagent")
    assert loaded.task_id == "a"
    assert loaded.next == "派单任务 b"


def test_handoff_verify_detects_drift():
    tmp = _tmp()
    coord = _plan(tmp)
    _accept(coord, "a")
    handoff = build(coord, "a")
    assert verify(handoff, coord) == []
    coord.execute(C.CancelTask(task_id="b", reason="不再需要"))
    problems = verify(handoff, coord)
    assert any("b" in p and "状态变了" in p for p in problems)


def test_landing_requires_accepted_deps():
    from ohmyagent.integrate import land

    tmp = _tmp()
    coord = _plan(tmp)
    with raises(ConflictError):
        land(coord, "b")


def test_landing_order_respects_deps():
    from ohmyagent.integrate import order_landing, unaccepted_deps

    tmp = _tmp()
    coord = _plan(tmp)
    order = order_landing(coord.records(), ["a", "b"])
    assert order.ready == []
    assert order.waiting == ["a", "b"]
    assert unaccepted_deps(coord.records(), "b") == ["a"]
    _accept(coord, "a")
    order = order_landing(coord.records(), ["a", "b"])
    assert order.ready == ["a"]
    assert order.waiting == ["b"]
    assert unaccepted_deps(coord.records(), "b") == []
    _accept(coord, "b")
    assert order_landing(coord.records(), ["b", "a"]).ready == ["a", "b"]


def test_reopen_invalidates_downstream():
    from ohmyagent.integrate import reopen

    tmp = _tmp()
    coord = _plan(tmp)
    _accept(coord, "a")
    _accept(coord, "b")
    reopen(coord, "a", reason="契约变更")
    assert coord.record("a").status is TaskStatus.BLOCKED
    assert coord.record("b").status is TaskStatus.BLOCKED


def test_cli_creates_task(tmp_path=None):
    from ohmyagent.cli import main

    tmp = Path(tempfile.mkdtemp())
    code = main(
        ["--root", str(tmp), "task", "create", "--id", "t1", "--intent", "命令行登记"]
    )
    assert code == 0
    assert Coordinator(tmp / ".superagent").record("t1") is not None


def test_cli_status_runs_as_module():
    tmp = Path(tempfile.mkdtemp())
    from ohmyagent.cli import main

    main(["--root", str(tmp), "task", "create", "--id", "t1", "--intent", "登记"])
    proc = subprocess.run(
        [sys.executable, "-m", "ohmyagent", "--root", str(tmp), "status"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert "accepted" in proc.stdout


def test_mock_worker_writes_output():
    from ohmyagent.workers import MockWorker, WorkSpec

    tmp = Path(tempfile.mkdtemp())
    worker = MockWorker()
    result = worker.run(
        WorkSpec(task_id="a", intent="写一个文件", workspace=str(tmp))
    )
    assert Path(result.candidate).exists()


def test_mock_worker_injects_failure():
    from ohmyagent.workers import MockWorker, WorkSpec, WorkerError

    tmp = Path(tempfile.mkdtemp())
    worker = MockWorker(fail_on=1)
    with raises(WorkerError):
        worker.run(WorkSpec(task_id="a", intent="会失败", workspace=str(tmp)))
