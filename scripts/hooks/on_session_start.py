"""SessionStart hook 同期処理 (0.8.0 — Cozo 単独経路).

実行内容:
1. boot 層 (persona / rule) 全件を additionalContext で注入 (Cozo)
2. condense 閾値チェック (50 facts / 5000 tokens 超なら stderr 警告)
3. Ollama prewarm + 各種 Cozo backfill を detach 起動

fail-open: 例外 / Cozo 未初期化なら exit 0 で素通し.
"""
from __future__ import annotations

import json
import sys

from scripts.boot.inject import (
    condense_warning_if_needed_cozo, format_boot_facts,
)
from scripts.db_cozo.connection import init_db
from scripts.db_cozo.fact_persist import (
    clear_boot_dirty, fetch_boot_facts,
)
from scripts.db_cozo.wire import cozo_db_path_for, cozo_db_present
from scripts.hooks.spawn import (
    spawn_cozo_graph_backfill, spawn_cozo_topic_summary_backfill,
    spawn_prewarm,
)
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
    if not cozo_db_present(db_path):
        sys.stderr.write(
            "[persona-memory] Cozo DB が見つかりません (SessionStart). "
            "/persona-memory:init もしくは /persona-memory:upgrade を実行してください.\n",
        )
        return 0

    cozo_path = cozo_db_path_for(db_path)
    text = ""
    try:
        client = init_db(cozo_path)
        warn = condense_warning_if_needed_cozo(client)
        if warn:
            sys.stderr.write(warn)
        facts = fetch_boot_facts(client)
        text = format_boot_facts(facts)
        clear_boot_dirty(client)
    except Exception as e:
        sys.stderr.write(f"[persona-memory] boot inject failed: {e}\n")

    _emit_additional_context(text)

    # Ollama heavy + embed prewarm (fail-open)
    spawn_prewarm()

    # Cozo backfill 自動発火 (detach, fail-open)
    spawn_cozo_topic_summary_backfill(db_path)
    spawn_cozo_graph_backfill(db_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
