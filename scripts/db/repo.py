"""DB アクセスヘルパ — phase 2 では episodes の raw 保存のみ。"""
from __future__ import annotations

import sqlite3


def save_episode(
    conn: sqlite3.Connection,
    role: str,
    content: str,
    session_id: str,
    topic_id: str | None = None,
) -> int:
    """生発話を episodes に保存し、生成された episode_id を返す。

    topic_id 解決:
    - 明示指定があればそれを使う
    - 無ければ scripts.topic.state.get_active_topic で解決
    - PERSONA_TOPIC_DISABLE=1 の場合は NULL のまま (= topic 機能 bypass)
    どの場合でも episode は必ず保存される (fail-open).
    """
    from scripts.topic.state import ensure_topic, get_active_topic, topic_disabled

    if topic_id is None and not topic_disabled():
        try:
            topic_id = get_active_topic(conn, session_id)
            ensure_topic(conn, topic_id)
        except Exception:
            topic_id = None  # FK 違反 / 旧スキーマ → bypass
    cur = conn.execute(
        "INSERT INTO episodes(role, content, session_id, topic_id) VALUES (?, ?, ?, ?)",
        (role, content, session_id, topic_id),
    )
    conn.commit()
    return cur.lastrowid


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()
