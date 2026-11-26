"""Worker 执行器的抽象、真实 CLI 适配器与注册表。

- :class:`WorkSpec` / :class:`WorkResult`：主控与 Worker 之间唯一的结构化契约；
- :class:`WorkerError`：带分类（exit / timeout / environment / parse）的失败；
- :class:`ExecWorker`：把任意原生 CLI 的 stdout 事件流解析成结果，并抓取原生会话 ID；
- :class:`MockWorker`：离线可复现实现；
- :func:`worker_view`：给正在执行的 Worker 看的受限视图。
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


class WorkerError(Exception):
    """Worker 未能产出可判定结果。kind 说明失败属于哪一类。"""

    kinds = ("exit", "timeout", "environment", "parse")

    def __init__(self, message: str, *, kind: str = "exit", session: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.session = session


LEGACY_SELF_REASON = "旧标识 self 表示当时的主控，派单需要指定实际 Worker 入口"


def reject_legacy_self(name: str) -> None:
    if name == "self":
        raise WorkerError(LEGACY_SELF_REASON, kind="environment")


@dataclass(frozen=True)
class WorkSpec:
    """一次派单的内容：任务单。"""

    task_id: str
    intent: str
    workspace: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    effort: str = ""
    on_session: Callable[[str], None] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class WorkResult:
    candidate: str
    summary: str = ""
    evidence: str = ""
    session: str = ""
    exit_code: int = 0
    stderr: str = ""
    events: tuple[dict[str, Any], ...] = ()


class Worker(Protocol):
    name: str

    def run(self, spec: WorkSpec, *, timeout: float | None = None) -> WorkResult: ...


# ---- mock harness -----------------------------------------------------------


class MockWorker:
    """离线可复现的 Worker：把任务写成 JSON 文件，并返回固定的候选信息。"""

    name = "mock"

    def __init__(
        self,
        *,
        fail_on: int = 0,
        fail_kind: str = "exit",
        sleep_factory: Any = None,
        session: str = "",
    ) -> None:
        self.fail_on = fail_on
        self.fail_kind = fail_kind
        self.sleep_factory = sleep_factory
        self.session = session
        self.calls: list[WorkSpec] = []

    def run(self, spec: WorkSpec, *, timeout: float | None = None) -> WorkResult:
        self.calls.append(spec)
        if self.fail_on and len(self.calls) == self.fail_on:
            raise WorkerError(
                "模拟失败：Worker 在本次尝试中未产出可验收结果",
                kind=self.fail_kind,
            )
        if self.sleep_factory is not None:
            self.sleep_factory(timeout)
        root = Path(spec.workspace)
        out_dir = root / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        written = out_dir / f"{spec.task_id.replace('/', '_')}.json"
        written.write_text(
            json.dumps(
                {
                    "task": spec.task_id,
                    "intent": spec.intent,
                    "outputs": list(spec.outputs),
                    "write_scope": list(spec.write_scope),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return WorkResult(
            candidate=str(written),
            summary=f"完成 {spec.intent}",
            evidence=f"file://{written}",
            session=self.session,
        )


# ---- 真实 CLI 适配器 --------------------------------------------------------


@dataclass
class ExecConfig:
    """原生 CLI 调用配置。模型和推理档参数中的 `{value}` 替换为本次请求值。"""

    name: str
    argv: tuple[str, ...]
    resume_argv: tuple[str, ...] = ()
    session_pattern: str = ""
    prompt_flag: str = ""
    model_args: tuple[str, ...] = ()
    effort_args: tuple[str, ...] = ()


def _default_configs() -> dict[str, ExecConfig]:
    return {
        "codex": ExecConfig(
            name="codex",
            argv=("codex", "exec", "--json"),
            resume_argv=("codex", "exec", "resume", "--json"),
            session_pattern=r'"thread_id"\s*:\s*"([^"]+)"',
            prompt_flag="--",
            model_args=("--model", "{value}"),
            effort_args=("-c", "model_reasoning_effort={value}"),
        ),
        "gemini": ExecConfig(
            name="gemini",
            argv=("gemini", "--output-format", "json"),
            session_pattern=r'"session_id"\s*:\s*"([^"]+)"',
            prompt_flag="-p",
            model_args=("--model", "{value}"),
        ),
        "cursor": ExecConfig(
            name="cursor",
            argv=("cursor", "--output-format", "json"),
            session_pattern=r'"session_id"\s*:\s*"([^"]+)"',
            prompt_flag="-p",
            model_args=("--model", "{value}"),
        ),
    }


class ExecWorker:
    """把任意 CLI 的 stdout 逐行 JSON 事件流解析为结构化结果。

    - 启动进程，读取 stdout 并提取原生会话 ID；
    - 超时：杀掉进程树并按 ``timeout`` 分类；
    - 退出码非零：按 ``exit`` 分类，保留 stderr 供主控写进失败证据；
    - 找可执行文件失败：按 ``environment`` 分类。
    """

    kinds = WorkerError.kinds

    def __init__(
        self,
        *,
        name: str,
        build_args: Callable[[WorkSpec], list[str]],
        session_pattern: str = "",
        prompt: str | None = None,
        cwd: str | None = None,
        on_start: Callable[[int], None] | None = None,
    ) -> None:
        reject_legacy_self(name)
        self.name = name
        self.build_args = build_args
        self.session_pattern = session_pattern
        self.prompt = prompt
        self.cwd = cwd
        self.on_start = on_start

    @classmethod
    def from_config(cls, config: ExecConfig, *, prompt_suffix: str = "") -> ExecWorker:
        def build(spec: WorkSpec) -> list[str]:
            argv = list(config.argv)
            for value, template, field_name in (
                (spec.model, config.model_args, "模型"),
                (spec.effort, config.effort_args, "推理档"),
            ):
                if value:
                    if not template:
                        raise WorkerError(f"{config.name} 未配置显式{field_name}参数", kind="environment")
                    argv.extend(arg.replace("{value}", value) for arg in template)
            if config.prompt_flag:
                prompt = prompt_suffix or spec.intent
                if spec.outputs:
                    prompt += "\n预期产物：" + json.dumps(spec.outputs, ensure_ascii=False)
                if "review_scope" in spec.constraints:
                    prompt += "\n冻结的审查范围（逐项按原方法核验）：" + json.dumps(spec.constraints["review_scope"], ensure_ascii=False)
                argv += [config.prompt_flag, prompt]
            return argv

        return cls(
            name=config.name,
            build_args=build,
            session_pattern=config.session_pattern,
        )

    def run(self, spec: WorkSpec, *, timeout: float | None = None) -> WorkResult:
        args = self.build_args(spec)
        workspace = self.cwd or spec.workspace
        if workspace:
            try:
                Path(workspace).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise WorkerError(
                    f"无法准备工作区 {workspace}：{exc}", kind="environment"
                ) from exc
        try:
            proc = subprocess.Popen(
                args,
                cwd=workspace or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE if self.prompt is None else subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            raise WorkerError(f"无法启动 {self.name}：{exc}", kind="environment") from exc

        # Codex 等 CLI 会继续读取 stdin，关闭输入端让它们处理 argv 中的任务提示。
        if proc.stdin is not None:
            proc.stdin.close()
        if self.on_start is not None:
            self.on_start(proc.pid)

        events: list[dict[str, Any]] = []
        session = ""
        stderr_parts: list[str] = []
        timed_out = {"value": False}

        def drain() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_parts.append(line)

        def kill() -> None:
            timed_out["value"] = True
            _terminate_tree(proc)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        timer = threading.Timer(timeout, kill) if timeout is not None else None
        if timer:
            timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                payload: dict[str, Any] | None = None
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    loaded = None
                if isinstance(loaded, dict):
                    payload = loaded
                    events.append(loaded)
                    if not session and self.session_pattern:
                        match = re.search(self.session_pattern, line)
                        if match:
                            session = match.group(1)
                            if spec.on_session:
                                spec.on_session(session)
                else:
                    events.append({"type": "raw", "text": line})
            proc.wait()
        except BaseException:
            _terminate_tree(proc)
            raise
        finally:
            if timer:
                timer.cancel()
            reader.join(timeout=5)

        if timed_out["value"]:
            raise WorkerError(
                f"{self.name} 达到显式硬时限（{timeout:g}s），请核对原尝试的预期产物", kind="timeout", session=session
            )
        stderr = "".join(stderr_parts)
        if proc.returncode != 0:
            raise WorkerError(
                f"{self.name} 退出码 {proc.returncode}：{stderr.strip()[:400]}",
                kind="exit",
            )

        candidate, summary = _interpret(events, spec)
        if not candidate:
            candidate = _guess_candidate(workspace or spec.workspace, spec.task_id)
        if not candidate:
            raise WorkerError(f"{self.name} 没有给出可定位的产物", kind="parse")
        return WorkResult(
            candidate=candidate,
            summary=summary,
            evidence=f"{self.name}:{spec.task_id}",
            session=session,
            exit_code=proc.returncode or 0,
            stderr=stderr,
            events=tuple(events),
        )


def _terminate_tree(proc: subprocess.Popen[Any]) -> None:
    try:
        if os.name == "nt":
            if proc.poll() is not None:
                return
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=5,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if proc.poll() is None:
        proc.kill()


def _interpret(events: list[dict[str, Any]], spec: WorkSpec) -> tuple[str, str]:
    """从事件流里找产物路径与摘要。

    - ``{"type": "output_path", "path": ...}`` / ``{"type": "file"|"artifact", "path": ...}``
    - ``{"type": "agent_message", "text": ...}`` 作为摘要
    - ``{"type": "message", "content": [{"type": "output_path", ...}]}``
    """
    candidate = ""
    summary = ""
    for event in events:
        kind = event.get("type")
        if kind in ("output_path", "file", "artifact", "output") and isinstance(
            event.get("path"), str
        ):
            candidate = event["path"]
        elif kind in ("agent_message", "assistant_message") and isinstance(
            event.get("text"), str
        ):
            summary = event["text"]
        elif kind == "message" and isinstance(event.get("content"), list):
            for block in event["content"]:
                if isinstance(block, dict) and block.get("type") in (
                    "output_path",
                    "file",
                ):
                    candidate = str(block.get("path", ""))
    if not candidate and spec.outputs:
        candidate = spec.outputs[0]
    return candidate, summary


def _guess_candidate(workspace: str, task_id: str) -> str:
    """Worker 只用事件流给出了摘要、没有显式产物路径时，按约定位置找候选文件。

    约定：先找工作区下的 ``out/``，再找工作区根目录下的第一个普通文件。
    """
    base = Path(workspace)
    for root in (base / "out", base):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                return str(path)
    return ""


# ---- 注册表 ------------------------------------------------------------------


_REGISTRY: dict[str, Any] = {}


def register(worker: Any) -> None:
    reject_legacy_self(worker.name)
    if worker.name == "mock":
        raise ValueError("mock 是保留名")
    _REGISTRY[worker.name] = worker


def get(name: str) -> Any:
    if name == "mock":
        return MockWorker()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise WorkerError(f"没有名为 {name} 的 Worker", kind="environment") from None


def names() -> list[str]:
    return ["mock", *sorted(_REGISTRY)]


def default_configs() -> dict[str, ExecConfig]:
    return _default_configs()


__all__ = [
    "ExecConfig",
    "ExecWorker",
    "MockWorker",
    "Worker",
    "WorkerError",
    "WorkResult",
    "WorkSpec",
    "default_configs",
    "get",
    "names",
    "register",
]


def worker_view(coord: Any, task_id: str) -> dict[str, Any]:
    """给正在执行的 Worker 看的受限视图。

    其它任务的进度、验收结论、抢占关系一律不出现，避免 Worker 被全局状态干扰。
    实现见 :mod:`ohmyagent.view`。
    """
    from ..view import worker_view as _view

    return _view(coord, task_id)
