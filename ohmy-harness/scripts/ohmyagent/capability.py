"""探测本机执行者能不能被调用。

两件事：

1. :func:`classify` / :func:`probe`：离线判定可执行文件与登录态是否存在；
2. :func:`probe_with_worker`：用一次**真实调用**（假 CLI 或真 CLI）验证结论，
   声明可用但其实起不来时，结论必须跟着实际调用走。

只做只读探测，不发网络请求。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .workers import LEGACY_SELF_REASON, reject_legacy_self

EXECUTORS = ("codex", "gemini", "cursor", "mock")

AUTH_HINTS = {
    "codex": (".codex/auth.json",),
    "cursor": (".cursor/cli-config.json", ".cursor/cli.json"),
    "gemini": (".gemini/settings.json", ".gemini/config.json"),
}


@dataclass
class ExecutorStatus:
    executor: str
    status: str  # available / unavailable / offline
    command: str = ""
    reason: str = ""
    auth: bool | None = None
    gap: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor": self.executor,
            "status": self.status,
            "command": self.command,
            "reason": self.reason,
            "auth": self.auth,
            "gap": self.gap,
        }


def locate(executor: str) -> str:
    if executor == "cursor":
        return shutil.which("cursor-agent") or shutil.which("cursor") or ""
    if executor == "self":
        return ""  # 旧报告使用的主控角色名，即使 PATH 上有同名程序也不解析。
    if executor == "mock":
        return "in-process"
    return shutil.which(executor) or ""


def _auth(executor: str, home: Path) -> bool | None:
    hints = AUTH_HINTS.get(executor)
    if not hints:
        return None
    return any((home / hint).exists() for hint in hints)


def classify(executor: str, home: Path | None = None) -> ExecutorStatus:
    if executor == "self":
        return ExecutorStatus(executor, "unavailable", reason=LEGACY_SELF_REASON)
    home = home or Path.home()
    command = locate(executor)
    if executor == "mock":
        return ExecutorStatus("mock", "offline", command, "进程内模拟执行器")
    if not command:
        return ExecutorStatus(
            executor, "unavailable", "", "可执行文件不在 PATH 中", None,
            f"{executor} 未安装或未加入 PATH",
        )
    auth = _auth(executor, home)
    if auth is False:
        return ExecutorStatus(
            executor, "unavailable", command, "有可执行文件但没有登录态文件", False,
            f"{executor} 已安装但未登录",
        )
    return ExecutorStatus(executor, "available", command, "", auth)


def probe(root: Path | None = None, home: Path | None = None) -> dict[str, Any]:
    executors = [classify(name, home) for name in EXECUTORS]
    gaps = [e.gap for e in executors if e.gap]
    return {
        "root": str(root) if root else "",
        "executors": [e.to_dict() for e in executors],
        "gaps": gaps,
        "allow": {
            "allowed": sorted(e.executor for e in executors if e.status == "available"),
            "offline": sorted(e.executor for e in executors if e.status == "offline"),
            "unavailable": sorted(
                e.executor for e in executors if e.status == "unavailable"
            ),
        },
        "workers": [],
    }


def probe_with_worker(
    *,
    tmp: Path | None = None,
    worker: Any,
    limit: float = 5.0,
) -> dict[str, Any]:
    """用一次真实调用验证“能不能派”。结论以调用结果为准，不以文件存在为准。"""
    report = probe(tmp)
    from .workers import WorkSpec, WorkerError

    spec = WorkSpec(
        task_id="__probe__",
        intent="能力探测：请回一个空结果",
        workspace=str((tmp or Path.cwd()) / ".superagent" / "probe"),
        outputs=(str((tmp or Path.cwd()) / ".superagent" / "probe" / "probe.txt"),),
    )
    try:
        Path(spec.workspace).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    entry: dict[str, Any]
    try:
        reject_legacy_self(getattr(worker, "name", ""))
        result = worker.run(spec, timeout=limit)
        entry = {
            "executor": getattr(worker, "name", "unknown"),
            "status": "available",
            "command": " ".join(getattr(worker, "args", [])[:2]),
            "reason": "",
            "auth": None,
            "gap": "",
            "evidence": result.candidate,
            "session": result.session,
        }
    except WorkerError as exc:
        entry = {
            "executor": getattr(worker, "name", "unknown"),
            "status": "unavailable",
            "command": " ".join(getattr(worker, "args", [])[:2]),
            "reason": str(exc),
            "auth": None,
            "gap": f"{getattr(worker, 'name', 'worker')} 实际调用失败：{exc.kind}",
            "evidence": "",
            "session": "",
        }
    report["workers"] = [entry]
    return report


def write(report: dict[str, Any], state: Path) -> Path:
    path = Path(state) / "capability.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


__all__ = [
    "EXECUTORS",
    "ExecutorStatus",
    "classify",
    "locate",
    "probe",
    "probe_with_worker",
    "write",
]
