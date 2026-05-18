"""server/db.py の vec 検索が現行 schema (fact_embeddings / episode_embeddings)
で動くことの回帰テスト.

旧命名 (facts_vec / episodes_vec) のままだと "no such table: facts_vec" で
MCP search_memory が常時失敗していたため、 同症状を防ぐ guard test.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlite_vec

from server import db as server_db


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch):
    """現行 schema.sql + 現行命名 virtual table で初期化した一時 DB."""
    path = tmp_path / "test.db"
    repo_root = Path(__file__).resolve().parent.parent
    schema_sql = (repo_root / "scripts" / "db" / "schema.sql").read_text()

    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(schema_sql)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fact_embeddings USING vec0("
        "fact_id INTEGER PRIMARY KEY, embedding FLOAT[768])"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS episode_embeddings USING vec0("
        "episode_id INTEGER PRIMARY KEY, embedding FLOAT[768])"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("PERSONA_MEMORY_DB", str(path))
    return path


def _ones() -> bytes:
    """768 次元の正規ベクトルもどき (テスト用 dummy embedding)."""
    import struct
    return struct.pack(f"{768}f", *([1.0 / (768 ** 0.5)] * 768))


def test_search_facts_uses_fact_embeddings_table(fresh_db):
    """search_facts が "no such table" を出さずに動く."""
    # 1 件 fact を upsert + embedding 登録
    fid = server_db.upsert_fact(
        category="preference", key="coffee", value="深煎り",
        importance=5, source=None,
    )
    server_db.write_fact_embedding(fid, _ones())
    # search_facts が table 不在 で例外を出さない
    results = server_db.search_facts(
        embedding_blob=_ones(), top_k=5, category=None,
    )
    assert isinstance(results, list)
    assert len(results) >= 1
    assert results[0]["value"] == "深煎り"


def test_search_episodes_uses_episode_embeddings_table(fresh_db):
    """search_episodes が "no such table" を出さずに動く."""
    # episodes に直接 insert (server.db.append_episode は別 column 不整合あり)
    conn = sqlite3.connect(fresh_db)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    cur = conn.execute(
        "INSERT INTO episodes(session_id, role, content) VALUES (?, ?, ?) "
        "RETURNING id",
        ("s1", "user", "今日の議題"),
    )
    eid = cur.fetchone()[0]
    conn.execute(
        "INSERT INTO episode_embeddings(episode_id, embedding) VALUES (?, ?)",
        (eid, _ones()),
    )
    conn.commit()
    conn.close()
    results = server_db.search_episodes(embedding_blob=_ones(), top_k=5)
    assert isinstance(results, list)
    assert len(results) >= 1


def test_find_neighbors_uses_fact_embeddings_table(fresh_db):
    fid1 = server_db.upsert_fact(
        category="preference", key="a", value="x", importance=5, source=None,
    )
    fid2 = server_db.upsert_fact(
        category="preference", key="b", value="y", importance=5, source=None,
    )
    server_db.write_fact_embedding(fid1, _ones())
    server_db.write_fact_embedding(fid2, _ones())
    neighbors = server_db.find_neighbors(fid1, _ones(), top_k=5)
    assert isinstance(neighbors, list)
    # 自分自身は除外されるため fid2 が返る
    assert any(n["fact_id"] == fid2 for n in neighbors)


def test_delete_fact_removes_embedding_row(fresh_db):
    """delete_fact が fact_embeddings から削除する経路の回帰."""
    fid = server_db.upsert_fact(
        category="preference", key="del_test", value="v",
        importance=5, source=None,
    )
    server_db.write_fact_embedding(fid, _ones())
    out = server_db.delete_fact(fact_id=fid)
    assert out is not None
    # 削除後、 search に乗らない
    results = server_db.search_facts(
        embedding_blob=_ones(), top_k=10, category=None,
    )
    assert not any(r["id"] == fid for r in results)
