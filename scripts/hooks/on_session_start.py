"""SessionStart hook 同期処理 (phase 8 範囲).

実行内容:
1. boot 層 (persona / rule) 全件を additionalContext で注入
2. condense 閾値チェック (50 facts / 5000 tokens 超なら stderr 警告)
3. 未処理 episode 再抽出 detach 起動 (耐障害性: 前回 write が落ちても次回起動時に救済)

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
from scripts.hooks.spawn import (
    spawn_prewarm, spawn_topic_summary_backfill, spawn_write,
)
from scripts.shared.env import get_db_path
from scripts.write.run import fetch_unprocessed_episode_ids


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

    pending_ids: list[int] = []
    try:
        warn = condense_warning_if_needed(conn)
        if warn:
            sys.stderr.write(warn)

        facts = fetch_boot_facts(conn)
        text = format_boot_facts(facts)
        # SessionStart で再注入したら dirty はクリア
        clear_dirty(conn)

        # 未処理 episode の検出 (前回 write が落ちた場合のリカバリ)
        pending_ids = fetch_unprocessed_episode_ids(conn)
    finally:
        conn.close()

    _emit_additional_context(text)

    # Ollama heavy + embed モデルを background で warm-up.
    # 最初の発話時の cold start (30-60s) を回避. fail-open.
    spawn_prewarm()

    # 0.6.24: summary 未生成の topic を 1 件遡及生成 (背景, fail-open).
    spawn_topic_summary_backfill()

    # 未処理があれば detach で write を流す (本処理 = 注入は完了済み)
    if pending_ids:
        sys.stderr.write(
            f"[persona-memory] resuming {len(pending_ids)} unprocessed episode(s) "
            f"from previous session\n"
        )
        spawn_write(pending_ids)

    return 0


if __name__ == "__main__":
    sys.exit(main())
