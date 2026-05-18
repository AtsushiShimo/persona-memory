"""SessionEnd hook (0.8.0 — Cozo 単独経路).

session 終了時に当該 session の active topic に対して summary を update.
topic_summary.update_summary は 0.7.6 で実装済 (Cozo 直接).

PERSONA_TOPIC_DISABLE=1 / PERSONA_TOPIC_SUMMARY_DISABLE=1 で skip.
fail-open: 例外は呑んで exit 0.
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    if os.environ.get("PERSONA_TOPIC_DISABLE", "").strip() == "1":
        return 0
    if os.environ.get("PERSONA_TOPIC_SUMMARY_DISABLE", "").strip() == "1":
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    session_id = payload.get("session_id") or ""
    if not session_id:
        return 0

    try:
        from scripts.db_cozo.connection import init_db
        from scripts.db_cozo.repo import get_active_topic
        from scripts.db_cozo.wire import cozo_db_path_for, cozo_db_present
        from scripts.shared.env import get_db_path
    except Exception:
        return 0

    db_path = get_db_path()
    if db_path is None or not cozo_db_present(db_path):
        return 0
    try:
        client = init_db(cozo_db_path_for(db_path))
        topic_id = get_active_topic(client, session_id)
        if not topic_id:
            return 0
        # last_active_at を touch (= 「終了直前まで使われていた」 印)
        from scripts.db_cozo.repo import touch_topic
        touch_topic(client, topic_id)
    except Exception as e:
        sys.stderr.write(f"[persona-memory] session_end failed: {e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
