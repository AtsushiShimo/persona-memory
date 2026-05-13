"""DB リポジトリ関数のテスト。"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import get_meta, save_episode, set_meta


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


def test_save_episode_returns_id(db):
    eid = save_episode(db, role="user", content="hello", session_id="s1")
    assert isinstance(eid, int)
    assert eid > 0


def test_save_episode_persists(db):
    save_episode(db, role="user", content="first", session_id="s1")
    save_episode(db, role="assistant", content="reply", session_id="s1")
    rows = db.execute(
        "SELECT role, content, session_id FROM episodes ORDER BY id"
    ).fetchall()
    assert rows == [("user", "first", "s1"), ("assistant", "reply", "s1")]


def test_meta_get_set(db):
    assert get_meta(db, "missing") is None
    set_meta(db, "raw_save_last_idx:s1", "5")
    assert get_meta(db, "raw_save_last_idx:s1") == "5"
    set_meta(db, "raw_save_last_idx:s1", "12")  # upsert
    assert get_meta(db, "raw_save_last_idx:s1") == "12"


# ── 0.6.24 トピック記憶: save_episode が topic_id を解決 ─────────────────

def test_save_episode_defaults_topic_id_to_session_id(db, monkeypatch):
    """継承指示が無ければ topic_id = session_id, topics 行も自動作成."""
    monkeypatch.delenv("PERSONA_TOPIC_DISABLE", raising=False)
    eid = save_episode(db, role="user", content="x", session_id="s-99")
    row = db.execute("SELECT topic_id FROM episodes WHERE id=?", (eid,)).fetchone()
    assert row[0] == "s-99"
    n = db.execute("SELECT COUNT(*) FROM topics WHERE id='s-99'").fetchone()[0]
    assert n == 1


def test_save_episode_respects_continue_topic_override(db, monkeypatch):
    """set_active_topic で session→既存 topic_id を継承させた場合, 以後の episode は
    その topic_id に紐付く."""
    monkeypatch.delenv("PERSONA_TOPIC_DISABLE", raising=False)
    from scripts.topic.state import set_active_topic
    db.execute("INSERT INTO topics(id, title) VALUES ('past-topic', '前の話題')")
    db.commit()
    set_active_topic(db, "s-new", "past-topic")
    eid = save_episode(db, role="user", content="続きですわ", session_id="s-new")
    row = db.execute("SELECT topic_id FROM episodes WHERE id=?", (eid,)).fetchone()
    assert row[0] == "past-topic"


def test_save_episode_topic_disable_keeps_topic_id_null(db, monkeypatch):
    """PERSONA_TOPIC_DISABLE=1 の時は topic_id NULL, topics 行も作らない."""
    monkeypatch.setenv("PERSONA_TOPIC_DISABLE", "1")
    eid = save_episode(db, role="user", content="x", session_id="s-disabled")
    row = db.execute("SELECT topic_id FROM episodes WHERE id=?", (eid,)).fetchone()
    assert row[0] is None
    n = db.execute("SELECT COUNT(*) FROM topics WHERE id='s-disabled'").fetchone()[0]
    assert n == 0
