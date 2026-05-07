"""SessionStart hook 同期処理 (phase 5 範囲).

実行内容:
1. boot 層 (persona / rule) 全件を additionalContext で注入
2. condense 閾値チェック (50 facts / 5000 tokens 超なら stderr 警告)
3. 未処理 episode 再抽出 detach 起動 ← phase 8 で追加

fail-open: 例外 / 未初期化 DB は exit 0 で素通し。
"""
from __future__ import annotations

import json
import sys

from scripts.boot.inject import (
    clear_dirty,
    condense_warning_if_needed,
    fetch_boot_facts,
    format_boot_facts,
)
from scripts.db.connection import connect
from scripts.shared.env import get_db_path


def _emit_additional_context(text: str) -> None:
    if not text:
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


def main() -> int:
    db_path = get_db_path()
    if db_path is None:
        return 0

    try:
        conn = connect(db_path)
    except Exception as e:
        sys.stderr.write(f"[persona-memory] db open failed: {e}\n")
        return 0

    try:
        warn = condense_warning_if_needed(conn)
        if warn:
            sys.stderr.write(warn)

        facts = fetch_boot_facts(conn)
        text = format_boot_facts(facts)
        # SessionStart で再注入したら dirty はクリア
        clear_dirty(conn)
    finally:
        conn.close()

    _emit_additional_context(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
