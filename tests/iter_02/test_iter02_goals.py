"""迭代 2 的目标测试：Worker 适配器、失败分类、计划版本、执行状态受限视图。

在迭代 1 结束时的代码上运行应失败，改造通过后应全绿。
"""

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner import raises  # noqa: E402
from ohmyagent import ConflictError, Coordinator, UnknownTaskError  # noqa: E402
from ohmyagent.store import commands as C  # noqa: E402

# 一个假 Worker CLI：把会话 ID 打到 stdout，等待读取再返回
FAKE_CLI = r'''
import json, sys
from pathlib import Path
mode = sys.argv[1]
args = sys.argv[2:]
out_dir = Path(args[0])
if mode == "hang":
    import time
    time.sleep(30)
if mode == "fail":
    print("boom", file=sys.stderr)
    print(json.dumps({"type": "error", "message": "boom"}))
    raise SystemExit(3)
session = json.dumps({"type": "thread.started", "thread_id": "thread-abc"})
message = json.dumps({"type": "agent_message", "text": "done"})
print(session, flush=True)
print(message, flush=True)
writer = out_dir / "result.txt"
writer.parent.mkdir(parents=True, exist_ok=True)
writer.write_text("ok", encoding="utf-8")
'''

FAKE_NAME = "fake-exec.py"


def _tmp() -> Path:
    root = Path(tempfile.mkdtemp(prefix="oma-iter02-"))
    (root / FAKE_NAME).write_text(FAKE_CLI, encoding="utf-8")
    return root


def _coord(root: Path) -> Coordinator:
    return Coordinator(root / ".superagent")


# ---- 目标 1：真实 CLI 适配器（stdout JSON 事件流） -------------------------


def test_exec_adapter_captures_session_and_output():
    from ohmyagent.workers import ExecWorker

    root = _tmp()
    worker = ExecWorker(
        name="fake",
        build_args=lambda spec: [
            sys.executable,
            str(root / FAKE_NAME),
            "ok",
            spec.workspace,
        ],
        session_pattern=r'"thread_id"\s*:\s*"([^"]+)"',
    )
    result = worker.run(
        __import__("ohmyagent.workers", fromlist=["WorkSpec"]).WorkSpec(
            task_id="a", intent="x", workspace=str(root / "wt")
        )
    )
    assert result.session == "thread-abc"
    assert result.exit_code == 0
    assert (root / "wt" / "result.txt").exists()
    assert result.candidate.endswith("result.txt")


def test_exec_adapter_times_out_without_produce():
    from ohmyagent.workers import ExecWorker, WorkSpec, WorkerError

    root = _tmp()
    worker = ExecWorker(
        name="fake",
        build_args=lambda spec: [sys.executable, str(root / FAKE_NAME), "hang",
                                 spec.workspace],
    )
    with raises(WorkerError):
        worker.run(
            WorkSpec(task_id="a", intent="x", workspace=str(root / "wt")), timeout=1.0
        )


def test_exec_adapter_reports_non_zero_exit():
    from ohmyagent.workers import ExecWorker, WorkSpec, WorkerError

    root = _tmp()
    worker = ExecWorker(
        name="fake",
        build_args=lambda spec: [sys.executable, str(root / FAKE_NAME), "fail",
                                 spec.workspace],
    )
    with raises(WorkerError) as _:
        worker.run(WorkSpec(task_id="a", intent="x", workspace=str(root / "wt")))


def test_exec_adapter_failure_kind_is_recorded():
    from ohmyagent.workers import ExecWorker, WorkSpec, WorkerError

    root = _tmp()
    worker = ExecWorker(
        name="fake",
        build_args=lambda spec: [sys.executable, str(root / FAKE_NAME), "fail",
                                 spec.workspace],
    )
    try:
        worker.run(WorkSpec(task_id="a", intent="x", workspace=str(root / "wt")))
    except WorkerError as exc:
        assert exc.kind == "exit"


def test_dispatch_use_worker_records_session():
    from ohmyagent.workers import ExecWorker

    root = _tmp()
    coord = _coord(root)
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(root)))
    worker = ExecWorker(
        name="fake",
        build_args=lambda spec: [sys.executable, str(root / FAKE_NAME), "ok",
                                 spec.workspace],
        session_pattern=r'"thread_id"\s*:\s*"([^"]+)"',
    )
    event = coord.execute(
        C.DispatchTask(task_id="a", session="", workspace=str(root / "wt"), use_worker=worker)
    )
    record = coord.record("a")
    attempt = record.attempt(record.active_attempt)
    assert attempt.session == "thread-abc"
    assert event.data["session"] == "thread-abc"


# ---- 目标 2：失败分类（业务失败 / 环境失败 / 超时） ------------------------


def test_failure_classification_for_exit():
    from ohmyagent.workers import WorkerError

    assert WorkerError("x", kind="exit").kind == "exit"
    assert WorkerError("x", kind="timeout").kind == "timeout"


def test_dispatch_use_worker_failure_marks_blocked_with_kind():
    from ohmyagent.workers import WorkerError

    class BrokenWorker:
        name = "broken"

        def run(self, spec, *, timeout=300.0):
            raise WorkerError("没有可执行文件", kind="environment")

    root = _tmp()
    coord = _coord(root)
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(root)))
    coord.execute(
        C.DispatchTask(task_id="a", session="", use_worker=BrokenWorker(), workspace=str(root / "wt"))
    )
    record = coord.record("a")
    assert record.status.value == "blocked"
    assert record.constraints["failure_kind"] == "environment"


# ---- 目标 3：计划版本与分母差异 --------------------------------------------


def test_plan_version_lists_leaf_tasks():
    root = _tmp()
    coord = _coord(root)
    coord.execute(C.CreateTask(task_id="a", intent="契约", project_root=str(root)))
    coord.execute(
        C.CreateTask(task_id="b", intent="服务端", project_root=str(root), deps=("b1",))
    )
    coord.execute(
        C.CreateTask(task_id="b1", intent="服务端细则", project_root=str(root), deps=("a",))
    )
    coord.execute(
        C.CreateTask(task_id="c", intent="SDK", project_root=str(root), deps=("a",))
    )
    coord.execute(
        C.CreatePlan(task_id="p", leaf_tasks=("a", "b", "b1", "c"), members=("a", "b", "b1", "c"))
    )
    from ohmyagent.report.progress import project_progress

    # 分母是纳入本轮、且未被其它在册成员依赖的任务：a 与 b 被依赖，只剩 b1、c
    progress = project_progress(coord, plan="p")
    assert progress["total"] == 2, progress
    assert progress["members"] == 4, progress
    assert progress["accepted"] == 0
    assert progress["version"] == 1


def test_plan_membership_change_records_difference():
    root = _tmp()
    coord = _coord(root)
    for tid in ("b", "c", "d"):
        coord.execute(C.CreateTask(task_id=tid, intent=tid, project_root=str(root)))
    coord.execute(C.CreatePlan(task_id="p", leaf_tasks=("b", "c")))
    coord.execute(C.UpdatePlan(task_id="p", add=("d",), remove=("c",)))
    from ohmyagent.report.progress import project_progress

    progress = project_progress(coord, plan="p")
    assert progress["total"] == 2
    assert progress["version"] == 2
    assert progress["missing"] == []
    assert progress["differences"]["added"] == ["d"]
    assert progress["differences"]["removed"] == ["c"]


def test_plan_withdrawn_leaf_is_removed_from_numerator():
    root = _tmp()
    coord = _coord(root)
    coord.execute(C.CreateTask(task_id="b", intent="b", project_root=str(root)))
    coord.execute(C.CreateTask(task_id="c", intent="c", project_root=str(root)))
    coord.execute(C.CreatePlan(task_id="p", leaf_tasks=("b", "c")))
    coord.execute(C.DispatchTask(task_id="b", session="s"))
    coord.execute(C.SubmitResult(task_id="b", attempt=1, candidate="c"))
    coord.execute(C.Verify(task_id="b", outcome="pass"))
    from ohmyagent.report.progress import project_progress

    progress = project_progress(coord, plan="p")
    assert progress["accepted"] == 1 and progress["total"] == 2
    coord.execute(C.UpdatePlan(task_id="p", remove=("b",), reason="验收撤回"))
    progress = project_progress(coord, plan="p")
    assert progress["accepted"] == 0 and progress["total"] == 1


# ---- 目标 4：执行中的 Worker 看不到验收方内部的进度 ----------------------


def test_worker_view_only_sees_own_attempt():
    from ohmyagent.workers import worker_view

    root = _tmp()
    coord = _coord(root)
    coord.execute(C.CreateTask(task_id="a", intent="x", project_root=str(root)))
    coord.execute(C.CreateTask(task_id="b", intent="y", project_root=str(root)))
    coord.execute(C.DispatchTask(task_id="a", session="s-a"))
    coord.execute(C.DispatchTask(task_id="b", session="s-b"))
    view = worker_view(coord, "a")
    assert [t["task"] for t in view["tasks"]] == ["a"]
    assert view["tasks"][0]["attempt"] == 1
    assert "b" not in [t["task"] for t in view["tasks"]]


def test_worker_view_rejects_unknown_task():
    root = _tmp()
    coord = _coord(root)
    from ohmyagent.workers import worker_view

    with raises(UnknownTaskError):
        worker_view(coord, "nope")
