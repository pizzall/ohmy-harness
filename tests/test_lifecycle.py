import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner import fixture, raises  # noqa: E402  (noqa: F401)
from ohmyagent import (  # noqa: E402
    ConflictError,
    Coordinator,
    CorruptionError,
    DuplicateTaskError,
    TaskStatus,
    UnknownTaskError,
)
from ohmyagent.store import commands as C  # noqa: E402


@fixture()
def tmp_path():
    import tempfile

    return Path(tempfile.mkdtemp(prefix="oma-life-"))


def _coord(tmp: Path) -> Coordinator:
    return Coordinator(tmp / ".superagent")


def _new(tmp: Path, task_id: str = "a", **kwargs):
    coord = _coord(tmp)
    cmd = C.CreateTask(task_id=task_id, intent="做一件事", project_root=str(tmp), **kwargs)
    coord.execute(cmd)
    return coord


def test_create_is_pending(tmp_path):
    coord = _new(tmp_path)
    assert coord.record("a").status is TaskStatus.PENDING


def test_dispatch_only_from_pending(tmp_path):
    coord = _new(tmp_path)
    coord.execute(C.DispatchTask(task_id="a", session="codex:s1"))
    assert coord.record("a").status is TaskStatus.IN_PROGRESS
    with raises(ConflictError):
        coord.execute(C.DispatchTask(task_id="a", session="codex:s2"))


def test_submit_only_from_in_progress(tmp_path):
    coord = _new(tmp_path)
    with raises(ConflictError):
        coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c0"))
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c1"))
    assert coord.record("a").status is TaskStatus.VERIFYING


def test_verify_pass_accepts(tmp_path):
    coord = _new(tmp_path)
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c1"))
    coord.execute(C.Verify(task_id="a", outcome="pass", check="单元测试"))
    record = coord.record("a")
    assert record.status is TaskStatus.ACCEPTED
    assert record.accepted_attempt == 1


def test_verify_failure_goes_blocked(tmp_path):
    coord = _new(tmp_path)
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c1"))
    coord.execute(C.Verify(task_id="a", outcome="rework", check="跨页遗漏"))
    assert coord.record("a").status is TaskStatus.BLOCKED


def test_rework_bumps_attempt(tmp_path):
    coord = _new(tmp_path)
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="c1"))
    coord.execute(C.Verify(task_id="a", outcome="rework"))
    coord.execute(C.BeginAttempt(task_id="a", session="s1"))
    record = coord.record("a")
    assert record.status is TaskStatus.IN_PROGRESS
    assert record.number == 2
    assert record.active_attempt == 2


def test_unknown_task_rejected(tmp_path):
    coord = _new(tmp_path)
    with raises(UnknownTaskError):
        coord.execute(C.DispatchTask(task_id="nope", session="s1"))


def test_duplicate_task_rejected(tmp_path):
    coord = _new(tmp_path)
    with raises(DuplicateTaskError):
        coord.execute(
            C.CreateTask(task_id="a", intent="重复", project_root=str(tmp_path))
        )


def test_sql_injection_like_id_is_plain_string(tmp_path):
    coord = _new(tmp_path, task_id='a"; DROP TABLE x; --')
    assert coord.record('a"; DROP TABLE x; --').status is TaskStatus.PENDING


def test_snapshot_is_read_only_view(tmp_path):
    coord = _new(tmp_path)
    snap = coord.snapshot("a")
    assert snap.status is TaskStatus.PENDING
    with raises(AttributeError):
        snap.status = "in_progress"  # type: ignore[misc]


def test_corrupt_middle_line_raises(tmp_path):
    coord = _new(tmp_path)
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    path = coord.ledger.record
    text = path.read_text(encoding="utf-8").splitlines()
    broken = text[0] + "\n{not json}\n" + "\n".join(text[1:]) + "\n"
    path.write_text(broken, encoding="utf-8")
    coord.invalidate()
    from ohmyagent.store.ledger import CorruptLedgerError

    with raises(CorruptLedgerError):
        coord.records()
