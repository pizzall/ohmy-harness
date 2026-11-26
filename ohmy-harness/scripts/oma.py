"""运行随 ohmy-harness 安装的账本。"""

import sys
from pathlib import Path


if __name__ == "__main__":
    scripts = Path(__file__).resolve().parent
    if not (scripts / "ohmyagent" / "cli.py").is_file():
        sys.exit("找不到配套账本。请将同版 ohmyagent 包安装到本技能的 scripts 目录。")
    sys.path.insert(0, str(scripts))
    from ohmyagent.cli import main

    raise SystemExit(main())
