"""0.7.9 復旧 migrate のテスト (legacy vec table + episodes.created_at 列)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

from scripts.upgrade import (
    _migrate_episodes_timestamp_column, _migrate_legacy_vec_tables,
)


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _make_legacy_vec_db(path: Path) -> sqlite3.Connection:
    """旧 init-memory.py 風: facts_vec / episodes_vec しか持たない DB."""
    conn = _open(path)
    conn.execute(
        "CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        "CREATE TABLE episodes (id INTEGER PRIMARY KEY, content TEXT)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE facts_vec USING vec0("
        "  fact_id INTEGER PRIMARY KEY, embedding FLOAT[768])"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE episodes_vec USING vec0("
        "  episode_id INTEGER PRIMARY KEY, embedding FLOAT[768])"
    )
    return conn


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (name,),
    ).fetchone() is not None


def test_migrate_facts_vec_transfers_rows_and_drops_old(tmp_path: Path):
    db = tmp_path / "legacy.db"
    conn = _make_legacy_vec_db(db)
    # facts_vec に 3 件入れる
    import struct
    vec = struct.pack(f"{768}f", *([0.1] * 768))
    for fid in (1, 2, 3):
        conn.execute("INSERT INTO facts(id, value) VALUES (?, ?)", (fid, f"v{fid}"))
        conn.execute(
            "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
            (fid, vec),
        )
    conn.commit()

    result = _migrate_legacy_vec_tables(conn)
    assert result["facts_vec_migrated"] == 3
    assert not _has_table(conn, "facts_vec")
    assert _has_table(conn, "fact_embeddings")
    n = conn.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()[0]
    assert n == 3
    conn.close()


def test_migrate_episodes_vec_transfers_rows(tmp_path: Path):
    db = tmp_path / "legacy.db"
    conn = _make_legacy_vec_db(db)
    import struct
    vec = struct.pack(f"{768}f", *([0.2] * 768))
    for eid in (1, 2):
        conn.execute("INSERT INTO episodes(id, content) VALUES (?, ?)", (eid, f"e{eid}"))
        conn.execute(
            "INSERT INTO episodes_vec(episode_id, embedding) VALUES (?, ?)",
            (eid, vec),
        )
    conn.commit()
    result = _migrate_legacy_vec_tables(conn)
    assert result["episodes_vec_migrated"] == 2
    assert not _has_table(conn, "episodes_vec")
    assert _has_table(conn, "episode_embeddings")
    conn.close()


def test_migrate_is_idempotent(tmp_path: Path):
    """既に新 table のみの DB に migrate → no-op."""
    db = tmp_path / "fresh.db"
    conn = _open(db)
    conn.execute(
        "CREATE VIRTUAL TABLE fact_embeddings USING vec0("
        "  fact_id INTEGER PRIMARY KEY, embedding FLOAT[768])"
    )
    conn.commit()
    result = _migrate_legacy_vec_tables(conn)
    assert result == {"facts_vec_migrated": 0, "episodes_vec_migrated": 0}
    conn.close()


def test_migrate_skips_when_both_old_and_new_exist(tmp_path: Path):
    """衝突を避けるため、 新 table が既にあれば旧 table を残して skip."""
    db = tmp_path / "conflict.db"
    conn = _open(db)
    conn.execute(
        "CREATE VIRTUAL TABLE facts_vec USING vec0("
        "  fact_id INTEGER PRIMARY KEY, embedding FLOAT[768])"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE fact_embeddings USING vec0("
        "  fact_id INTEGER PRIMARY KEY, embedding FLOAT[768])"
    )
    conn.commit()
    result = _migrate_legacy_vec_tables(conn)
    assert result["facts_vec_migrated"] == 0
    # どちらも残る (= データロス回避)
    assert _has_table(conn, "facts_vec")
    assert _has_table(conn, "fact_embeddings")
    conn.close()


def test_migrate_episodes_timestamp_adds_column(tmp_path: Path):
    """created_at しか無い episodes が rename-table 戦略で新 schema に置き換わり、
    値が timestamp にコピーされる. 旧 created_at 列は drop される."""
    db = tmp_path / "old_episodes.db"
    conn = _open(db)
    conn.execute(
        "CREATE TABLE episodes ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  role TEXT CHECK (role IN ('user','assistant')),"
        "  content TEXT,"
        "  created_at TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))"
        ")"
    )
    conn.execute(
        "INSERT INTO episodes(role, content, created_at) "
        "VALUES (?, ?, ?)", ("user", "x", "2026-05-01 10:00:00"),
    )
    conn.commit()
    changed = _migrate_episodes_timestamp_column(conn)
    assert changed is True
    cols = {row[1] for row in conn.execute("PRAGMA table_info(episodes)").fetchall()}
    assert "timestamp" in cols
    # rename-table 戦略で旧 created_at 列は新スキーマに含まれず drop される
    assert "created_at" not in cols
    # 値がコピー済
    ts = conn.execute("SELECT timestamp FROM episodes WHERE id=1").fetchone()[0]
    assert ts == "2026-05-01 10:00:00"
    conn.close()


def test_migrate_episodes_timestamp_idempotent(tmp_path: Path):
    """timestamp 列既存 DB に呼んでも no-op (= False 返す)."""
    db = tmp_path / "new_episodes.db"
    conn = _open(db)
    conn.execute(
        "CREATE TABLE episodes ("
        "  id INTEGER PRIMARY KEY,"
        "  timestamp TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))"
        ")"
    )
    conn.commit()
    changed = _migrate_episodes_timestamp_column(conn)
    assert changed is False
    conn.close()


def test_migrate_episodes_timestamp_neither_column_returns_false(tmp_path: Path):
    """どちらの列も無い壊れた DB は触らない (異常系の安全策)."""
    db = tmp_path / "broken.db"
    conn = _open(db)
    conn.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY)")
    conn.commit()
    changed = _migrate_episodes_timestamp_column(conn)
    assert changed is False
    conn.close()


def test_e2e_search_works_after_legacy_migrate(tmp_path: Path, monkeypatch):
    """旧 facts_vec DB → migrate → server.db.search_facts が動く."""
    db = tmp_path / "legacy_full.db"
    # 旧 init 風: schema は最小限 + facts_vec
    conn = _open(db)
    conn.executescript("""
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            importance INTEGER NOT NULL DEFAULT 5,
            status TEXT NOT NULL DEFAULT 'active',
            source TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
        );
    """)
    conn.execute(
        "CREATE VIRTUAL TABLE facts_vec USING vec0("
        "  fact_id INTEGER PRIMARY KEY, embedding FLOAT[768])"
    )
    # 1 件 fact + embedding
    import struct
    vec = struct.pack(f"{768}f", *([0.1] * 768))
    conn.execute(
        "INSERT INTO facts(category, key, value, importance) "
        "VALUES ('preference', 'coffee', '深煎り', 5)"
    )
    fid = conn.execute("SELECT id FROM facts").fetchone()[0]
    conn.execute(
        "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)", (fid, vec),
    )
    conn.commit()

    # migrate
    result = _migrate_legacy_vec_tables(conn)
    assert result["facts_vec_migrated"] == 1
    conn.close()

    # server.db で search が動く
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db))
    from server import db as server_db
    results = server_db.search_facts(embedding_blob=vec, top_k=5, category=None)
    assert len(results) >= 1
    assert results[0]["value"] == "深煎り"
