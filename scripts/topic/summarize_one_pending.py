"""SessionStart fallback CLI: summary 未生成の topic を 1 件だけ要約.

detach 子プロセスとして起動される. fail-open: 例外は呑んで exit 0.
"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        from scripts.db.connection import connect
        from scripts.shared.env import get_db_path
        from scripts.shared.ollama import OllamaClient
        from scripts.topic.summarize import (
            find_topic_needing_summary, summarize_topic,
        )
    except Exception:
        return 0
    db_path = get_db_path()
    if db_path is None:
        return 0
    try:
        conn = connect(db_path)
    except Exception:
        return 0
    try:
        topic_id = find_topic_needing_summary(conn)
        if not topic_id:
            return 0
        summarize_topic(conn, topic_id, OllamaClient())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
