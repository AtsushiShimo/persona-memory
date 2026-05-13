"""トピックID の active 状態管理.

設計:
- 既定では 1 セッション = 1 topic. session_id をそのまま topic_id として使う.
- main agent が「前回の続き」 と判断した時に MCP `continue_topic(topic_id)` を
  呼ぶと、 当該 session の active topic を別の (既存の) topic_id に上書きする.
- override は meta テーブルに `topic_for_session_<session_id>` キーで保存
  (DB に閉じるので、 並行セッションがあってもクラッシュしない).

PERSONA_TOPIC_DISABLE=1 の時は topic_id を発行せず NULL のまま episodes に
insert される (機能丸ごと bypass).
"""
from __future__ import annotations

import os
import sqlite3


def topic_disabled() -> bool:
    return os.environ.get("PERSONA_TOPIC_DISABLE", "").strip() == "1"


def get_active_topic(conn: sqlite3.Connection, session_id: str) -> str:
    """この session に紐付けられた topic_id を返す.

    継承指示が無ければ session_id そのまま.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?",
        (f"topic_for_session_{session_id}",),
    ).fetchone()
    return row[0] if row else session_id


def set_active_topic(conn: sqlite3.Connection, session_id: str, topic_id: str) -> None:
    """continue_topic で session の active topic を上書き."""
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (f"topic_for_session_{session_id}", topic_id),
    )
    conn.commit()


def ensure_topic(conn: sqlite3.Connection, topic_id: str) -> None:
    """topic 行が無ければ作る (idempotent). 同時に last_active_at を更新.

    FK (episodes.topic_id REFERENCES topics.id) を満たすため save_episode 直前に呼ぶ.
    """
    conn.execute("INSERT OR IGNORE INTO topics(id) VALUES (?)", (topic_id,))
    conn.execute(
        "UPDATE topics SET last_active_at = datetime('now', '+9 hours') WHERE id=?",
        (topic_id,),
    )
    conn.commit()
