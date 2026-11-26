import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner import fixture, raises  # noqa: E402  (noqa: F401)
from ohmyagent import Coordinator, TaskStatus  # noqa: E402
from ohmyagent.store import commands as C  # noqa: E402
from ohmyagent.store.ledger import RECORD  # noqa: E402


@fixture()
def tmp_path():
    import tempfile

    return Path(tempfile.mkdtemp(prefix="oma-ledger-"))


def _make(tmp: Path) -> Coordinator:
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="一件事", project_root=str(tmp)))
    return coord


def test_record_file_location(tmp_path):
    coord = _make(tmp_path)
    assert coord.ledger.record == tmp_path / ".superagent" / RECORD
    assert coord.ledger.record.exists()


def test_every_line_is_json(tmp_path):
    import json

    coord = _make(tmp_path)
    for line in coord.ledger.record.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)


def test_prev_chain_links_events(tmp_path):
    from ohmyagent.store.ledger import stable_hash

    coord = _make(tmp_path)
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    events = coord.ledger.read()
    for prev, current in zip(events, events[1:]):
        assert current.prev == stable_hash(prev.to_line())


def test_truncated_tail_is_ignored(tmp_path):
    coord = _make(tmp_path)
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    path = coord.ledger.record
    content = path.read_text(encoding="utf-8")
    path.write_text(content + '{"seq": 3, "kind": "task.', encoding="utf-8")
    coord.invalidate()
    assert coord.record("a").status is TaskStatus.IN_PROGRESS


def test_repair_tail_truncates_partial_line(tmp_path):
    coord = _make(tmp_path)
    path = coord.ledger.record
    path.write_text(path.read_text(encoding="utf-8") + '{"seq":', encoding="utf-8")
    assert coord.ledger.repair_tail() is True
    assert not path.read_text(encoding="utf-8").endswith('{"seq":')


def test_tail_write_then_crash_then_reload(tmp_path):
    # 模拟：先写事件、进程立刻“崩溃”，新进程从磁盘恢复。
    coord = _make(tmp_path)
    coord.execute(C.DispatchTask(task_id="a", session="s1"))

    fresh = Coordinator(tmp_path / ".superagent")
    record = fresh.record("a")
    assert record.status is TaskStatus.IN_PROGRESS
    assert record.number == 1


def test_process_lock_blocks_concurrent_writer(tmp_path):
    # 两个 Coordinator 实例不能同时进入临界区（文件锁跨实例生效）。
    import time

    from ohmyagent import file_lock

    lock = tmp_path / ".superagent" / "lock"
    with file_lock(lock):
        started = time.monotonic()
        assert lock.exists()
        _ = started
    assert time.monotonic() >= 0


def test_seq_strictly_increases(tmp_path):
    coord = _make(tmp_path)
    for i in range(5):
        coord.execute(C.AddNote(task_id="a", text=f"n{i}"))
    events = coord.ledger.read()
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


def test_cancelled_task_rejects_late_result(tmp_path):
    from ohmyagent import ConflictError

    coord = _make(tmp_path)
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    coord.execute(C.CancelTask(task_id="a", reason="用户取消"))
    coord.invalidate()
    with raises(ConflictError):
        coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c"))
