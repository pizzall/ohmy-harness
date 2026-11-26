"""纯 ASCII，避免控制台代码页把日志写花。"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner import Suite, report, run  # noqa: E402


def main(argv: list[str]) -> int:
    paths = argv or [str(ROOT / "tests")]
    suite: Suite = run(paths)
    code = report(suite)
    log = ROOT / "build" / "last-run.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{r.status.upper():7} {r.name}" for r in suite.results]
    lines.append(suite.summary())
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[log] {log}")
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception:  # pragma: no cover
        traceback.print_exc()
        raise SystemExit(2)
