"""最小离线测试运行器：不依赖 pytest / 第三方库。

运行入口为 python tests/all.py [路径]。
- 文件 tests/test_*.py 或 tests/iter_*/test_*.py；
- 其中 test_* 函数为用例；
- 支持 fixtures（@fixture）、xfail、参数化（@parametrize）。"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

RUNTIME = Path(__file__).resolve().parents[1] / "ohmy-harness" / "scripts"
sys.path.insert(0, str(RUNTIME))
# 并发与 CLI 用例会启动独立解释器，让它们也测试本次发布的账本。
os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, (str(RUNTIME), os.environ.get("PYTHONPATH"))))
os.environ["PYTHONIOENCODING"] = "utf-8"


class Skipped(Exception):
    pass


class XFailed(Exception):
    pass


@dataclass
class Result:
    name: str
    status: str  # passed / failed / xfailed / skipped
    detail: str = ""


@dataclass
class Suite:
    results: list[Result] = field(default_factory=list)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "passed")

    def summary(self) -> str:
        return (
            f"passed={self.passed} failed={self.failed} "
            f"skipped={sum(1 for r in self.results if r.status == 'skipped')} "
            f"xfailed={sum(1 for r in self.results if r.status == 'xfailed')}"
        )


# ---- 装饰器 -----------------------------------------------------------------


def fixture(*args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn._oma_fixture = True  # type: ignore[attr-defined]
        return fn

    return wrap


def parametrize(name: str, values: list[Any]) -> Callable[[Callable[..., Any]], Any]:
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        marks = getattr(fn, "_params", [])
        fn._params = marks + [(name, list(values))]  # type: ignore[attr-defined]
        return fn

    return wrap


def xfail(fn: Callable[..., Any]) -> Callable[..., Any]:
    fn._xfail = True  # type: ignore[attr-defined]
    return fn


def skip(reason: str = "") -> Callable[[Callable[..., Any]], Any]:
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn._skip = reason or "skipped"  # type: ignore[attr-defined]
        return fn

    return wrap


def raises(exc: type[BaseException]) -> Any:
    class _Ctx:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, et: Any, ev: Any, tb: Any) -> bool:
            if et is None:
                raise AssertionError(f"期望抛出 {exc.__name__}")
            return issubclass(et, exc)

    return _Ctx()


# ---- 收集与运行 -------------------------------------------------------------


def _modules(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in paths:
        if root.is_file() and root.suffix == ".py" and root.name.startswith("test_"):
            files.append(root)
        for dirpath, _dirs, filenames in os.walk(root):
            for name in sorted(filenames):
                if name.startswith("test_") and name.endswith(".py"):
                    files.append(Path(dirpath) / name)
    return sorted(set(files))


def _load(path: Path) -> Any:
    name = f"oma_test_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.modules[path.stem] = module  # 让 multiprocessing(spawn) 能重新导入测试模块
    spec.loader.exec_module(module)
    return module


def _fixtures(module: Any, tmp: Path) -> dict[str, Any]:
    values: dict[str, Any] = {"tmp_path": tmp}
    for name, obj in vars(module).items():
        if getattr(obj, "_oma_fixture", False):
            try:
                values[name] = obj() if _takes(obj) == 0 else obj(**{})
            except TypeError:
                values[name] = obj()
    return values


def _takes(fn: Callable[..., Any]) -> int:
    return len([p for p in inspect.signature(fn).parameters.values()])


def _expand(fn: Callable[..., Any]) -> list[dict[str, Any]]:
    marks = getattr(fn, "_params", [])
    cases: list[dict[str, Any]] = [{}]
    for name, values in marks:
        next_cases = []
        for case in cases:
            for value in values:
                next_cases.append({**case, name: value})
        cases = next_cases
    return cases


def run(paths: list[str] | None = None) -> Suite:
    roots = [Path(p).resolve() for p in (paths or [str(Path(__file__).parent)])]
    suite = Suite()
    for path in _modules(roots):
        module = _load(path)
        for name, fn in sorted(vars(module).items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            if getattr(fn, "__module__", None) != module.__name__:
                continue  # 只收集本文件定义的用例，不收集 import 进来的
            for case in _expand(fn):
                # 每条用例一个新的临时目录，保证用例之间互不干扰
                tmp = Path(tempfile.mkdtemp(prefix="oma-test-"))
                defaults = _fixtures(module, tmp)
                kwargs = {**defaults, **case}
                params = inspect.signature(fn).parameters
                kwargs = {k: v for k, v in kwargs.items() if k in params}
                label = f"{path.name}::{name}"
                if case:
                    label += f"[{','.join(f'{k}={v!r}' for k, v in case.items())}]"
                if getattr(fn, "_skip", None):
                    suite.results.append(Result(label, "skipped", fn._skip))
                    continue
                try:
                    fn(**kwargs)
                    suite.results.append(Result(label, "passed"))
                except Exception as exc:  # noqa: BLE001
                    if getattr(fn, "_xfail", False):
                        suite.results.append(Result(label, "xfailed", str(exc)))
                    else:
                        detail = "".join(traceback.format_exception_only(type(exc), exc))
                        suite.results.append(Result(label, "failed", detail.strip()))
    return suite


def report(suite: Suite) -> int:
    for r in suite.results:
        marker = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP", "xfailed": "XFAIL"}[
            r.status
        ]
        print(f"{marker:6} {r.name}")
        if r.detail and r.status != "passed":
            for line in r.detail.splitlines():
                print(f"       {line}")
    print("-" * 60)
    print(suite.summary())
    return 1 if suite.failed else 0
