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
