"""SessionEnd hook (0.6.24): 当該 session の topic を要約して保存.

detached でも構わないが (ユーザー視点ではセッションは既に終わっている)、
ここでは sync で 1 LLM コールだけ投げて UPDATE する. heavy LLM の所要は
通常 5-15s 程度.

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
        from scripts.db.connection import connect
        from scripts.shared.env import get_db_path
        from scripts.shared.ollama import OllamaClient
        from scripts.topic.summarize import find_topic_for_session, summarize_topic
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
        topic_id = find_topic_for_session(conn, session_id)
        if not topic_id:
            return 0
        client = OllamaClient()
        try:
            summarize_topic(conn, topic_id, client)
        except Exception as e:
            sys.stderr.write(f"[persona-memory] topic summarize failed: {e}\n")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
