import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runner import RUNTIME, raises  # noqa: E402
from ohmyagent import ConflictError, Coordinator, TaskStatus  # noqa: E402
from ohmyagent.store import commands as C  # noqa: E402
from ohmyagent.store.ledger import stable_hash  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="oma-iter03u-"))


# ---- 目标 1：超时核对与重派守卫 ------------------------------------------------


def test_redispatch_rejects_without_verdict():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    with raises(ConflictError):
        coord.execute(C.Redispatch(task_id="a", session="s2"))


def test_reconcile_rejects_non_active_attempt():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    coord.execute(C.ReconcileAttempt(task_id="a", attempt=1, verdict="dead"))
    coord.execute(C.Redispatch(task_id="a", session="s2"))
    with raises(ConflictError):
        coord.execute(C.ReconcileAttempt(task_id="a", attempt=1, verdict="dead"))


def test_reconcile_rejects_unknown_verdict():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    with raises(ConflictError):
        coord.execute(C.ReconcileAttempt(task_id="a", attempt=1, verdict="maybe"))


def test_scan_lists_timed_out_attempt_until_reconciled():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    coord.execute(C.DecideBlock(task_id="a", kind="timeout", reason="20s 超时"))
    item = coord.scan()["timed_out"][0]
    assert item["task"] == "a" and item["attempt"] == 1
    assert item["verdict"] == "pending"
    coord.execute(C.ReconcileAttempt(task_id="a", attempt=1, verdict="dead"))
    assert coord.scan()["timed_out"] == []
    assert [b["task"] for b in coord.scan()["blocked"]] == ["a"]


def test_capability_report_shape():
    from ohmyagent.capability import probe

    report = probe(Path("."))
    assert set(report["allow"]) == {"allowed", "offline", "unavailable"}
    names = [e["executor"] for e in report["executors"]]
    assert names == ["codex", "gemini", "cursor", "mock"]


# ---- 目标 2：多进程并发（独立 worker 模块 + spawn 子进程） ----------------------


WORKER_SOURCE = r'''
import sys
from pathlib import Path


def _bootstrap():
    for path in sys.argv[1:2]:
        if path not in sys.path:
            sys.path.insert(0, path)


def append_notes(state, task_id, rounds):
    from ohmyagent import Coordinator
    from ohmyagent.store import commands as C

    coord = Coordinator(Path(state))
    for i in range(rounds):
        coord.execute(C.AddNote(task_id=task_id, text=f"{task_id}-{i}"))


def contest(state, session, queue):
    from ohmyagent import Coordinator
    from ohmyagent.core.errors import ConflictError
    from ohmyagent.store import commands as C

    try:
        Coordinator(Path(state)).execute(
            C.SubmitResult(task_id="a", attempt=1, candidate=session)
        )
        queue.put("ok")
    except ConflictError:
        queue.put("conflict")


if __name__ == "__main__":
    _root = sys.argv[1]
    _bootstrap()
    command = sys.argv[2]
    if command == "append":
        append_notes(sys.argv[3], sys.argv[4], int(sys.argv[5]))
    elif command == "race":
        state = sys.argv[3]
        ctx = __import__("multiprocessing").get_context("spawn")
        proc = ctx.Process(target=contest, args=(state, "s", ctx.Queue()))
        proc.start()
        proc.join(60)
'''


def _worker(root: Path) -> Path:
    module = root / "oma_iter03_unit_worker.py"
    module.write_text(WORKER_SOURCE, encoding="utf-8")
    return module


def _call(module: Path, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(module), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=150,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
    return proc.stdout


def test_multi_process_appends_are_serialized():
    import concurrent.futures

    tmp = _tmp()
    module = _worker(tmp)
    state = tmp / ".superagent"
    coord = Coordinator(state)
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_call, module, str(ROOT), "append", str(state), "a", "10")
            for _ in range(3)
        ]
        for future in futures:
            future.result()
    events = Coordinator(state).ledger.read()
    assert len(events) == 31, len(events)
    assert [e.seq for e in events] == list(range(1, 32))
    for prev, cur in zip(events, events[1:]):
        assert cur.prev == stable_hash(prev.to_line())


def test_concurrent_submit_has_single_winner():
    import concurrent.futures

    tmp = _tmp()
    module = _worker(tmp)
    state = tmp / ".superagent"
    coord = Coordinator(state)
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_call, module, str(ROOT), "race", str(state), f"s{i}")
            for i in range(3)
        ]
        for future in futures:
            future.result()
    assert Coordinator(state).record("a").status is TaskStatus.VERIFYING


def test_cli_scan_and_capability_run():
    tmp = _tmp()
    base = [sys.executable, "-m", "ohmyagent", "--root", str(tmp)]
    for args in (["scan"], ["capability"]):
        proc = subprocess.run(
            base + args, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8"
        )
        assert proc.returncode == 0, proc.stderr
        json.loads(proc.stdout)
    assert (tmp / ".superagent" / "capability.json").exists()


def test_late_result_after_redispatch_is_archived():
    tmp = _tmp()
    coord = Coordinator(tmp / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(tmp)))
    coord.execute(C.DispatchTask(task_id="a", session="s1"))
    coord.execute(C.DecideBlock(task_id="a", kind="timeout"))
    coord.execute(C.ReconcileAttempt(task_id="a", attempt=1, verdict="dead"))
    coord.execute(C.Redispatch(task_id="a", session="s2"))
    event = coord.execute(C.SubmitResult(task_id="a", attempt=1, candidate="late"))
    record = coord.record("a")
    assert event.kind == "attempt.late_result"
    assert record.attempt(1).status == "late"
    assert record.status is TaskStatus.IN_PROGRESS


def test_no_replacement_characters_in_sources():
    for path in (RUNTIME / "ohmyagent").rglob("*.py"):
        assert "\ufffd" not in path.read_text(encoding="utf-8"), path
