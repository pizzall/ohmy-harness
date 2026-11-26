import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runner import raises  # noqa: E402
from ohmyagent import ConflictError, Coordinator, TaskStatus  # noqa: E402
from ohmyagent.store import commands as C  # noqa: E402
from ohmyagent.store.ledger import stable_hash  # noqa: E402

FAKE = r'''
import json, sys, time
mode = sys.argv[1]
out = sys.argv[2]
if mode == "slow":
    time.sleep(30)
print(json.dumps({"type": "thread.started", "thread_id": "t-1"}), flush=True)
if mode == "ok":
    print(json.dumps({"type": "agent_message", "text": "done"}), flush=True)
if out:
    with open(out, "w", encoding="utf-8") as writer:
        writer.write("ok")
'''

WORKER = r'''
import sys
from pathlib import Path

ROOT = Path(__ROOT__)


def bootstrap():
    for path in (str(ROOT), str(ROOT / "tests")):
        if path not in sys.path:
            sys.path.insert(0, path)


def append_notes(state, task_id, rounds, queue):
    bootstrap()
    try:
        from ohmyagent import Coordinator
        from ohmyagent.store import commands as C

        coord = Coordinator(Path(state))
        for i in range(rounds):
            coord.execute(C.AddNote(task_id=task_id, text=f"{task_id}-{i}"))
        queue.put("ok")
    except Exception as exc:
        queue.put(f"error:{exc!r}")


def contest(state, session, queue):
    bootstrap()
    try:
        from ohmyagent import Coordinator
        from ohmyagent.core.errors import ConflictError
        from ohmyagent.store import commands as C

        Coordinator(Path(state)).execute(
            C.SubmitResult(task_id="a", attempt=1, candidate=session)
        )
        queue.put("ok")
    except ConflictError:
        queue.put("conflict")
    except Exception as exc:
        queue.put(f"error:{exc!r}")


def main():
    bootstrap()
    import multiprocessing as mp

    mode, state, arg = sys.argv[1], sys.argv[2], sys.argv[3]
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    if mode == "append":
        target = append_notes
        args = (state, "a", int(arg), queue)
    else:
        target = contest
        args = (state, arg, queue)
    proc = ctx.Process(target=target, args=args)
    proc.start()
    result = queue.get(timeout=60)
    proc.join(30)
    print(result, flush=True)


if __name__ == "__main__":
    main()
'''


def _tmp() -> Path:
    root = Path(tempfile.mkdtemp(prefix="oma-iter03-"))
    (root / "fake.py").write_text(FAKE, encoding="utf-8")
    (root / "worker.py").write_text(
        WORKER.replace("__ROOT__", repr(str(ROOT))), encoding="utf-8"
    )
    return root


def _run_worker(root: Path, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(root / "worker.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=150,
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    return proc.stdout.strip().splitlines()[-1]


class Hanging:
    name = "hanging"

    def run(self, spec, *, timeout=300.0):
        from ohmyagent.workers import WorkerError

        raise WorkerError("超时", kind="timeout")


# ---- 目标 1：超时后先核对，确认结束后才允许重派 --------------------------------


def test_timeout_marks_dispatched_not_refunded():
    from ohmyagent.workers import ExecWorker

    root = _tmp()
    worker = ExecWorker(
        name="fake",
        build_args=lambda spec: [sys.executable, str(root / "fake.py"), "slow", ""],
    )
    coord = Coordinator(root / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(root)))
    coord.execute(C.DispatchTask(task_id="a", session="", use_worker=worker, timeout=0.5))
    record = coord.record("a")
    assert record.status.value == "blocked"
    assert record.constraints["failure_kind"] == "timeout"
    scan = coord.scan()
    assert [i["task"] for i in scan["dispatched_without_result"]] == []
    assert [i["task"] for i in scan["timed_out"]] == ["a"]


def test_reconcile_blocks_redispatch_while_running():
    root = _tmp()
    coord = Coordinator(root / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(root)))
    coord.execute(C.DispatchTask(task_id="a", session="", use_worker=Hanging()))
    coord.execute(C.ReconcileAttempt(task_id="a", attempt=1, verdict="running", by="main"))
    with raises(ConflictError):
        coord.execute(C.Redispatch(task_id="a", session="s2"))


def test_reconcile_allows_redispatch_after_confirmed_dead():
    root = _tmp()
    coord = Coordinator(root / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(root)))
    coord.execute(C.DispatchTask(task_id="a", session="", use_worker=Hanging()))
    coord.execute(C.ReconcileAttempt(task_id="a", attempt=1, verdict="dead", by="main"))
    coord.execute(C.Redispatch(task_id="a", session="s2"))
    record = coord.record("a")
    assert record.status.value == "in_progress"
    assert record.active_attempt == 2


def test_reconcile_keeps_late_result_of_old_attempt():
    root = _tmp()
    coord = Coordinator(root / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(root)))
    coord.execute(C.DispatchTask(task_id="a", session="", use_worker=Hanging()))
    coord.execute(C.ReconcileAttempt(task_id="a", attempt=1, verdict="dead", by="main"))
    coord.execute(C.Redispatch(task_id="a", session="s2"))
    event = coord.execute(
        C.SubmitResult(task_id="a", attempt=1, candidate="stale", summary="late")
    )
    record = coord.record("a")
    assert event.kind == "attempt.late_result"
    assert record.status.value == "in_progress"
    assert record.attempt(1).status == "late"


def test_reconcile_unknown_attempt_rejected():
    root = _tmp()
    coord = Coordinator(root / ".superagent")
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(root)))
    with raises(ConflictError):
        coord.execute(C.ReconcileAttempt(task_id="a", attempt=9, verdict="dead"))


# ---- 目标 2：多进程并发追加一致性 ---------------------------------------------


def test_multi_process_append_keeps_sequence_contiguous():
    root = _tmp()
    state = root / ".superagent"
    setup = Coordinator(state)
    setup.execute(C.CreateTask(task_id="a", intent="x", project_root=str(root)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_run_worker, root, "append", str(state), "15")
            for _ in range(3)
        ]
        assert [f.result() for f in futures] == ["ok", "ok", "ok"]

    events = Coordinator(state).ledger.read()
    assert len(events) == 46, len(events)
    assert [e.seq for e in events] == list(range(1, 47))
    for prev, cur in zip(events, events[1:]):
        assert cur.prev == stable_hash(prev.to_line())


def test_multi_process_same_attempt_race_has_single_winner():
    root = _tmp()
    state = root / ".superagent"
    setup = Coordinator(state)
    setup.execute(C.CreateTask(task_id="a", intent="x", project_root=str(root)))
    setup.execute(C.DispatchTask(task_id="a", session="s"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_run_worker, root, "race", str(state), f"s{i}")
            for i in range(4)
        ]
        outcomes = sorted(f.result() for f in futures)
    assert outcomes == ["conflict", "conflict", "conflict", "ok"], outcomes
    assert Coordinator(state).record("a").status is TaskStatus.VERIFYING


# ---- 目标 3：能力探测结论要与真实调用一致 --------------------------------------


def test_capability_follows_actual_invocation():
    from ohmyagent.capability import probe_with_worker
    from ohmyagent.workers import ExecWorker

    root = _tmp()
    worker = ExecWorker(
        name="declared",
        build_args=lambda spec: [sys.executable, str(root / "missing.py"), "ok", ""],
    )
    report = probe_with_worker(tmp=root, worker=worker, limit=30)
    entry = report["workers"][0]
    assert entry["status"] == "unavailable"
    assert entry["gap"]


def test_capability_marks_available_after_real_success():
    from ohmyagent.capability import probe_with_worker
    from ohmyagent.workers import ExecWorker

    root = _tmp()
    out = root / "result.txt"
    worker = ExecWorker(
        name="real",
        build_args=lambda spec: [sys.executable, str(root / "fake.py"), "ok", str(out)],
    )
    report = probe_with_worker(tmp=root, worker=worker, limit=60)
    assert report["workers"][0]["status"] == "available"
    assert out.exists()
    _ = os, json
