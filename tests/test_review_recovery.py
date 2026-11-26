import hashlib
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ohmyagent import Coordinator
from ohmyagent.cli import main
from ohmyagent.core.errors import ConflictError
from ohmyagent.handoff.session import build, verify as verify_handoff
from ohmyagent.store import commands as C
from ohmyagent.workers import ExecWorker, MockWorker
from runner import raises


def test_cold_start_registers_review_hook_without_creating_tasks():
    with TemporaryDirectory() as tmp:
        state = Path(tmp) / ".superagent"
        for _ in range(2):
            assert main(["--root", tmp, "init"]) == 0
            receipt = json.loads((state / "review-hook.json").read_text(encoding="utf-8"))
            assert (receipt["hook"], receipt["status"]) == ("review-scope", "ready")
            assert not Coordinator(state).ledger.read()


def test_failed_cold_start_invalidates_previous_registration_and_blocks_only_review_acceptance():
    with TemporaryDirectory() as tmp:
        state = Path(tmp) / ".superagent"
        coord = Coordinator(state)
        assert main(["--root", tmp, "init"]) == 0
        with patch.object(C, "check_coverage", return_value=[]):
            assert main(["--root", tmp, "init"]) != 0
        assert json.loads((state / "review-hook.json").read_text(encoding="utf-8"))["status"] == "failed"
        unit = {"source": "input", "method": "read", "criterion": "content"}
        scope = {"items": {"content": unit}, "excluded": []}
        (Path(tmp) / "input").write_text("ok", encoding="utf-8")
        coord.execute(C.CreateTask("audit", "read", tmp, role="review", constraints={"review_scope": scope}))
        coord.execute(C.DispatchTask("audit", "local"))
        coord.execute(C.SubmitResult("audit", 1, "v1"))
        proof = {"id": "content", "method": "read", "source": "input", "outcome": "pass", "finding": "read ok",
                 "sha256": hashlib.sha256(b"ok").hexdigest()}
        batch = {"task": "audit", "attempt": 1, "candidate": "v1", "items": [proof]}
        verify = C.Verify("audit", "pass", review_evidence=(batch,))
        with raises(ConflictError):
            coord.execute(verify)
        assert coord.record("audit").status.value == "verifying"
        coord.execute(C.CreateTask("other", "independent", tmp))
        assert main(["--root", tmp, "init"]) == 0
        coord.execute(verify)
        assert coord.record("audit").status.value == "accepted"


def test_direct_worker_is_observable_and_has_no_implicit_deadline():
    with TemporaryDirectory() as tmp:
        coord = Coordinator(Path(tmp) / ".superagent")
        coord.execute(C.CreateTask("slow", "long thought", tmp, outputs=("report.md",)))

        def observe(timeout):
            assert timeout is None
            observer = Coordinator(coord.state)
            item, = observer.scan()["dispatched_without_result"]
            assert item["outputs"] == ["report.md"]
            observer.execute(C.AddNote("slow", "still running"))

        result = coord.execute(C.DispatchTask("slow", "launch", use_worker=MockWorker(sleep_factory=observe)))
        assert result.data["candidate"]
        assert len(coord.record("slow").attempts) == 1


def test_timeout_recovers_expected_files_without_duplicate_attempt_or_early_acceptance():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        coord = Coordinator(root / ".superagent")
        coord.execute(C.CreateTask("slow", "two files", tmp, outputs=("a", "b")))
        coord.execute(C.DispatchTask("slow", "launch", use_worker=MockWorker(fail_on=1, fail_kind="timeout")))
        with raises(ConflictError):
            coord.execute(C.BeginAttempt("slow", "duplicate"))
        with raises(ConflictError):
            coord.execute(C.ResumeTask("slow"))
        assert coord.record("slow").attempt(1).status == "awaiting_reconciliation"
        assert Coordinator(coord.state).scan()["timed_out"][0]["outputs"] == ["a", "b"]
        (root / "a").write_text("first", encoding="utf-8")
        (root / "session.log").write_text("launch returned a and b", encoding="utf-8")
        outputs = {key: hashlib.sha256(value).hexdigest() for key, value in (("a", b"first"), ("b", b"second"))}
        receipt = {"task": "slow", "attempt": 1, "session": "launch", "outputs": outputs, "log": "session.log"}
        (root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        recovered = C.ReconcileAttempt("slow", 1, "recovered", "owner", candidate="a", evidence="receipt.json")
        with raises(ConflictError):
            coord.execute(recovered)
        (root / "b").write_text("stale", encoding="utf-8")
        with raises(ConflictError):
            coord.execute(recovered)
        (root / "b").write_text("second", encoding="utf-8")
        event = coord.execute(recovered)
        assert set(event.data["output_hashes"]) == {"a", "b"}
        record = Coordinator(coord.state).record("slow")
        assert record.status.value == "verifying" and record.number == 1 and record.accepted_attempt is None


def test_real_cli_hard_deadline_keeps_native_session_and_completed_file():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        coord = Coordinator(root / ".superagent")
        coord.execute(C.CreateTask("slow", "long thought", tmp, outputs=("result.txt",)))
        script = """
import time
from pathlib import Path
print('{"session":"native-42"}', flush=True)
Path("result.txt").write_text("ready", encoding="utf-8")
time.sleep(30)
"""
        worker = ExecWorker(name="fixture", build_args=lambda spec: [sys.executable, "-c", script], session_pattern=r'"session":"([^"]+)"')
        started = time.monotonic()
        coord.execute(C.DispatchTask("slow", "launch", use_worker=worker, timeout=2))
        assert time.monotonic() - started < 20
        item, = Coordinator(coord.state).scan()["timed_out"]
        assert item["session"] == "native-42" and (root / "result.txt").read_text() == "ready"
        assert any(event.data.get("kind") == "worker.session" for event in coord.ledger.read())


def test_uncertain_worker_keeps_write_scope_and_late_completion_cannot_revive_cancelled_task():
    with TemporaryDirectory() as tmp:
        coord = Coordinator(Path(tmp) / ".superagent")
        for task in ("first", "second"):
            coord.execute(C.CreateTask(task, task, tmp, write_scope=("shared",)))

        def observe(timeout):
            observer = Coordinator(coord.state)
            observer.execute(C.ReconcileAttempt("first", 1, "unknown"))
            with raises(ConflictError):
                observer.execute(C.DispatchTask("second", "overlap"))
            observer.execute(C.CancelTask("first", "cancel during execution"))

        coord.execute(C.DispatchTask("first", "launch", use_worker=MockWorker(sleep_factory=observe)))
        record = Coordinator(coord.state).record("first")
        assert record.status.value == "cancelled" and not record.attempt(1).candidate
        assert coord.ledger.read()[-1].data["candidate"]


def test_review_scope_is_required_and_cli_hook_checks_union_methods_and_versions():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        def cli(*args):
            return main(["--root", tmp, "task", *args])

        create = ("create", "--id", "audit", "--intent", "read both texts", "--role", "review")
        assert cli(*create) != 0
        items = {x: {"source": x, "method": "read-original", "criterion": "判断章节是否连贯"} for x in ("a", "b")}
        scope = {"items": items, "excluded": ["美术"]}
        (root / "scope.json").write_text(json.dumps(scope), encoding="utf-8")
        assert cli(*create, "--review-scope", str(root / "scope.json")) == 0
        assert cli("dispatch", "--id", "audit", "--session", "read") == 0
        for source in items:
            (root / source).write_text(source, encoding="utf-8")
        assert cli("submit", "--id", "audit", "--candidate", "revision-1") == 0
        coord = Coordinator(root / ".superagent")
        handoff = build(coord, "audit")
        assert handoff.unfinished[0]["review_scope"] == scope and not verify_handoff(handoff, coord)
        del handoff.unfinished[0]["review_scope"]
        assert verify_handoff(handoff, coord)
        verify = ("verify", "--id", "audit", "--outcome", "pass")
        assert cli(*verify, "--check", "reports exist") != 0

        def batch(item, method="read-original", attempt=1, candidate="revision-1", task="audit", outcome="pass"):
            source = root / item
            source.write_text(item, encoding="utf-8")
            proof = {"id": item, "source": item, "method": method, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                     "outcome": outcome, "finding": "复核段落衔接"}
            path = root / (item + ".json")
            path.write_text(json.dumps({"task": task, "attempt": attempt, "candidate": candidate, "items": [proof]}), encoding="utf-8")
            return str(path)

        first = batch("a")
        assert cli(*verify, "--review-evidence", first) != 0
        for bad in ({"method": "summary"}, {"attempt": 2}, {"candidate": "revision-0"}, {"task": "other"}, {"outcome": "fail"}):
            assert cli(*verify, "--review-evidence", first, "--review-evidence", batch("b", **bad)) != 0
        second = batch("b")
        args = (*verify, "--review-evidence", first, "--review-evidence", second)
        assert cli(*args, "--review-evidence", batch("extra")) != 0
        (root / "b").write_text("new revision", encoding="utf-8")
        assert cli(*args) != 0
        assert Coordinator(coord.state).record("audit").status.value == "verifying"
        batch("b")
        assert cli(*args) == 0
        event = coord.ledger.read()[-1]
        assert [p["id"] for p in event.data["review_items"]] == ["a", "b"]
