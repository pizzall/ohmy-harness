"""UTF-8 自检：防止源码被非 UTF-8 工具链二次编码。

按字节检查运行时代码和测试，检出解码错误与 U+FFFD 替换字符。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runner import RUNTIME, parametrize  # noqa: E402

FILES = sorted(
    [p for p in (RUNTIME / "ohmyagent").rglob("*.py")]
    + [p for p in (ROOT / "tests").rglob("*.py")]
)


@parametrize("path", FILES)
def test_source_is_clean_utf8(path):
    raw = path.read_bytes()
    text = raw.decode("utf-8")  # 解码失败即抛 UnicodeDecodeError
    assert "\ufffd" not in text, f"{path.name} 含替换字符，说明被非 UTF-8 工具写坏"


@parametrize("path", [p for p in (RUNTIME / "ohmyagent").rglob("*.py") if p.name != "__init__.py"])
def test_source_declares_utf8_when_needed(path):
    text = path.read_text(encoding="utf-8")
    if any(ord(ch) > 127 for ch in text):
        assert "from __future__ import annotations" in text or "coding" in text[:200].lower()
