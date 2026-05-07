#!/usr/bin/env python3
"""プロジェクト直下の .envrc に persona-memory 管理ブロックを add / remove.

仕様:
- マーカー (`# >>> persona-memory >>>` / `# <<< persona-memory <<<`) で囲んだ
  ブロックのみを管理する。マーカー外の行は触らない
- ペルソナ名は active-persona ファイルから動的に読む方式 (= ペルソナ切替に
  追従する。1 project = 1 persona の設計でも、reinit で名前が変わるケースに
  対応できる)
- 既存 .envrc にブロックが無い → 末尾追記
- 既存 .envrc にブロックがある → ブロック内のみ更新
- remove で .envrc が空になる → ファイル自体を削除
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

START_MARKER = "# >>> persona-memory >>>"
END_MARKER = "# <<< persona-memory <<<"

_BLOCK_BODY = """\
# Managed by persona-memory plugin. Do not edit between markers.
# Active persona name is read dynamically from .persona-memory/active-persona,
# so the prefix updates automatically when persona is switched.
if [ -f .persona-memory/active-persona ]; then
  export CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX="$(tr -d '\\n\\r' < .persona-memory/active-persona)"
fi
"""

FULL_BLOCK = f"{START_MARKER}\n{_BLOCK_BODY}{END_MARKER}\n"

# 既存ブロック検出用 (改行を含めた最小マッチ)
_BLOCK_RE = re.compile(
    re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER) + r"\n?",
    re.DOTALL,
)


def add_block(envrc: Path) -> str:
    """戻り値: 'created' / 'appended' / 'updated' / 'unchanged'."""
    if not envrc.exists():
        envrc.write_text(FULL_BLOCK, encoding="utf-8")
        return "created"

    text = envrc.read_text(encoding="utf-8")
    if _BLOCK_RE.search(text):
        # lambda で渡すことで re.sub の replacement バックスラッシュ解釈を回避
        # (FULL_BLOCK 内の '\\n\\r' リテラルを保持する)
        new_text = _BLOCK_RE.sub(lambda _m: FULL_BLOCK, text)
        if new_text == text:
            return "unchanged"
        envrc.write_text(new_text, encoding="utf-8")
        return "updated"

    sep = "" if text.endswith("\n") else "\n"
    envrc.write_text(text + sep + "\n" + FULL_BLOCK, encoding="utf-8")
    return "appended"


def remove_block(envrc: Path) -> str:
    """戻り値: 'removed' / 'file_deleted' / 'absent'."""
    if not envrc.exists():
        return "absent"

    text = envrc.read_text(encoding="utf-8")
    if not _BLOCK_RE.search(text):
        return "absent"

    # 前の改行も巻き込んで削除 (ブロック単位できれいに消す)
    pattern = re.compile(
        r"\n*" + re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER) + r"\n?",
        re.DOTALL,
    )
    new_text = pattern.sub("\n", text).lstrip("\n")

    if new_text.strip() == "":
        envrc.unlink()
        return "file_deleted"

    if not new_text.endswith("\n"):
        new_text += "\n"
    envrc.write_text(new_text, encoding="utf-8")
    return "removed"


def main() -> int:
    p = argparse.ArgumentParser(description="Manage persona-memory block in .envrc")
    p.add_argument("mode", choices=["add", "remove"])
    p.add_argument("envrc", type=Path)
    args = p.parse_args()

    if args.mode == "add":
        result = add_block(args.envrc)
    else:
        result = remove_block(args.envrc)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
