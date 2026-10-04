"""追跡対象のMarkdownリンクと初期化に必要な入口を検査する。"""

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

root = Path(__file__).resolve().parents[1]
required = (
    "README.md", "AGENTS.md", "CONTRIBUTING.md", "docs/repository-policy.md",
    ".coderabbit.yaml", ".github/pull_request_template.md",
)
for name in required:
    if not (root / name).is_file():
        raise SystemExit(f"必須ファイルなし: {name}")
paths = subprocess.check_output(
    ["git", "ls-files", "-z", "--", "*.md"], cwd=root,
).decode().split("\0")
count = 0
for name in filter(None, paths):
    path = root / name
    text = re.sub(r"```[^\n]*\n.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
            continue
        relative = unquote(target.split("#", 1)[0])
        if not (path.parent / relative).exists():
            raise SystemExit(f"リンク先なし: {name}: {target}")
    count += 1
print(f"リポジトリ入口・Markdown相対リンク: OK ({count} files)")
