import json
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ohmyagent import Coordinator
from ohmyagent.capability import classify, locate, probe_with_worker
from ohmyagent.store import commands as C
from ohmyagent.workers import ExecWorker, MockWorker, WorkerError, WorkSpec, default_configs, register
from runner import raises


CAPTURE_ARGS = """
import json
import os
import sys
import threading
from pathlib import Path

Path("argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
timeout = threading.Timer(2, lambda: os._exit(4))
timeout.daemon = True
timeout.start()
assert sys.stdin.read() == ""
"""


def test_direct_dispatch_passes_model_and_effort_to_codex_cli():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        coord = Coordinator(root / ".superagent")
        coord.execute(C.CreateTask("edit", "按任务单批改文案", str(root)))
        config = replace(default_configs()["codex"], argv=(sys.executable, "-c", CAPTURE_ARGS, "exec", "--json"))
        worker = ExecWorker.from_config(config)
        event = coord.execute(C.DispatchTask("edit", "", model="test-model", effort="low", use_worker=worker, timeout=5))

        args = json.loads((root / "argv.json").read_text(encoding="utf-8"))
        assert args == ["exec", "--json", "--model", "test-model", "-c", "model_reasoning_effort=low", "--", "按任务单批改文案"]
        assert event.data["model"] == "test-model"
        assert event.data["effort"] == "low"


def test_config_uses_native_defaults_for_unset_selection_and_rejects_unsupported_effort():
    config = default_configs()["gemini"]
    worker = ExecWorker.from_config(config)
    spec = WorkSpec("edit", "edit text", ".")
    assert worker.build_args(spec) == ["gemini", "--output-format", "json", "-p", "edit text"]
    assert worker.build_args(replace(spec, model="test-model")) == [
        "gemini", "--output-format", "json", "--model", "test-model", "-p", "edit text"
    ]
    with raises(WorkerError):
        worker.build_args(replace(spec, effort="low"))


def test_legacy_self_cannot_resolve_to_a_program_or_start_a_worker():
    with patch("ohmyagent.capability.shutil.which", return_value="unrelated-program"):
        assert locate("self") == ""
        assert classify("self").status == "unavailable"

    with raises(WorkerError):
        ExecWorker.from_config(replace(default_configs()["codex"], name="self"))
    worker = MockWorker()
    worker.name = "self"
    with raises(WorkerError):
        register(worker)
    with TemporaryDirectory() as directory:
        root = Path(directory)
        assert probe_with_worker(tmp=root, worker=worker)["workers"][0]["status"] == "unavailable"
        coord = Coordinator(root / ".superagent")
        coord.execute(C.CreateTask("edit", "修改文案", str(root)))
        coord.execute(C.DispatchTask("edit", "", use_worker=worker))
        assert coord.record("edit").constraints["failure_kind"] == "environment"
        assert worker.calls == []
