"""repo内Markdown相対リンクと必須の入口を確認する。"""

import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
required = ["README.md", "AGENTS.md", "CONTEXT.md", "docs/architecture.md",
            "docs/testing.md", "docs/adr/0001-voice-boundary.md"]
for name in required:
    assert (root / name).is_file(), name
for path in [root / name for name in required] + list((root / "docs").rglob("*.md")):
    for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
        if "://" not in target and not target.startswith("#"):
            assert (path.parent / target.split("#")[0]).exists(), f"{path}: {target}"
print("日本語docs入口・相対リンク: OK")
