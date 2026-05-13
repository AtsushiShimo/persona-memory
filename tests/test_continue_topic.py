"""MCP continue_topic (server.db.bind_session_to_topic) のテスト."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = tmp_path / "p.db"
    init_db(p)
    # server/db.py は config 経由でパスを引くので env で差し込む
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(p))
    # トピック機能が disabled な環境で走らせても save_episode が NULL を返さないよう
    # 明示的に有効化 (= 親環境変数の干渉を遮断).
    monkeypatch.delenv("PERSONA_TOPIC_DISABLE", raising=False)
    yield p


def test_bind_session_to_topic_with_explicit_session(db_path: Path):
    """session_id を明示すると、 meta に override が書かれる."""
    from server import db as server_db
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO topics(id, title) VALUES ('past', '過去議論')")
        conn.commit()
    finally:
        conn.close()

    out = server_db.bind_session_to_topic(session_id="s-now", topic_id="past")
    assert out == {"bound": True, "session_id": "s-now", "topic_id": "past"}

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='topic_for_session_s-now'"
        ).fetchone()
        assert row[0] == "past"
        # 以後の save_episode が past topic に紐付く
        eid = save_episode(conn, role="user", content="続きですわ", session_id="s-now")
        row = conn.execute("SELECT topic_id FROM episodes WHERE id=?", (eid,)).fetchone()
        assert row[0] == "past"
    finally:
        conn.close()


def test_bind_session_to_topic_auto_resolves_session_from_latest_episode(db_path: Path):
    """session_id 省略時は最新 episode の session_id を使う."""
    from server import db as server_db
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO topics(id) VALUES ('past')")
        conn.commit()
        # 最新 episode の session_id = 's-live'
        save_episode(conn, role="user", content="x", session_id="s-live")
    finally:
        conn.close()

    out = server_db.bind_session_to_topic(session_id=None, topic_id="past")
    assert out["bound"] is True
    assert out["session_id"] == "s-live"


def test_bind_session_to_topic_rejects_unknown_topic(db_path: Path):
    from server import db as server_db
    out = server_db.bind_session_to_topic(session_id="s1", topic_id="nope")
    assert "error" in out


def test_bind_session_to_topic_no_episodes_returns_error(db_path: Path):
    from server import db as server_db
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO topics(id) VALUES ('past')")
        conn.commit()
    finally:
        conn.close()
    out = server_db.bind_session_to_topic(session_id=None, topic_id="past")
    assert "error" in out
