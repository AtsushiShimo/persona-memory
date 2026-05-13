"""SQLite → Cozo migration のテスト."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from scripts.db.connection import EMBEDDING_DIM as SQL_EMB_DIM, connect as sql_connect
from scripts.db.migrate import init_db as sql_init
from scripts.db.repo import save_episode as sql_save_episode
from scripts.db_cozo.connection import init_db as cozo_init
from scripts.db_cozo.migrate_from_sqlite import migrate


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


@pytest.fixture
def src_db(tmp_path: Path) -> Path:
    p = tmp_path / "src.db"
    sql_init(p)
    conn = sql_connect(p)
    try:
        # facts
        cur = conn.execute(
            "INSERT INTO facts(category, key, value, importance) "
            "VALUES ('preference', 'coffee', '深煎り', 6)"
        )
        fid = cur.lastrowid
        emb = [0.0] * SQL_EMB_DIM
        emb[3] = 1.0
        conn.execute(
            "INSERT INTO fact_embeddings(fact_id, embedding) VALUES (?, ?)",
            (fid, _pack(emb)),
        )
        # episodes (with topic_id auto via save_episode -> topic 'sess1')
        sql_save_episode(conn, role="user", content="hello", session_id="sess1")
        sql_save_episode(conn, role="assistant", content="hi", session_id="sess1")
        # discussion nodes
        conn.execute(
            "INSERT INTO discussion_nodes(episode_id, kind, title, state) "
            "VALUES (1, 'topic', 'メンション設計', 'proposed')"
        )
        conn.execute(
            "INSERT INTO discussion_nodes(episode_id, kind, title, state) "
            "VALUES (2, 'decision', '種類分けを残す', 'accepted')"
        )
        # edge
        conn.execute(
            "INSERT INTO discussion_edges(src_id, dst_id, edge_kind) "
            "VALUES (1, 2, '決定')"
        )
        # meta
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('arbitrary_meta', 'X')"
        )
        conn.commit()
    finally:
        conn.close()
    return p


def test_migrate_copies_facts(src_db: Path, tmp_path: Path):
    dst = tmp_path / "dst.cozo.db"
    out = migrate(src_db, dst, progress=False)
    assert out["facts"] >= 1
    client = cozo_init(dst)
    rows = client.run("?[id, category, key, value] := *fact{id, category, key, value}")["rows"]
    assert any(r[2] == "coffee" and r[3] == "深煎り" for r in rows)


def test_migrate_copies_episodes_with_embedding(src_db: Path, tmp_path: Path):
    """旧 episodes は embedding 無しで save されるが、 episode_embeddings 経由で
    後付けされたものは vec として復元される."""
    dst = tmp_path / "dst.cozo.db"
    migrate(src_db, dst, progress=False)
    client = cozo_init(dst)
    rows = client.run("?[id, content] := *episode{id, content}")["rows"]
    assert len(rows) == 2


def test_migrate_copies_discussion_graph(src_db: Path, tmp_path: Path):
    dst = tmp_path / "dst.cozo.db"
    migrate(src_db, dst, progress=False)
    client = cozo_init(dst)
    nodes = client.run("?[id, title, kind, state] := *discussion_node{id, title, kind, state}")["rows"]
    assert len(nodes) == 2
    edges = client.run(
        "?[from_id, to_id, kind] := *discussion_edge{from_id, to_id, kind}"
    )["rows"]
    assert edges == [[1, 2, "決定"]]


def test_migrate_copies_topics_from_save_episode_side_effect(src_db: Path, tmp_path: Path):
    dst = tmp_path / "dst.cozo.db"
    migrate(src_db, dst, progress=False)
    client = cozo_init(dst)
    rows = client.run("?[id] := *topic{id}")["rows"]
    assert any(r[0] == "sess1" for r in rows)


def test_migrate_meta_preserves_arbitrary_keys_but_not_schema_version(src_db: Path, tmp_path: Path):
    dst = tmp_path / "dst.cozo.db"
    migrate(src_db, dst, progress=False)
    client = cozo_init(dst)
    rows = client.run("?[key, value] := *meta{key, value}")["rows"]
    by_key = {r[0]: r[1] for r in rows}
    assert by_key.get("arbitrary_meta") == "X"
    # schema_version は新側 (cozo-1) を保持
    assert by_key.get("schema_version") == "cozo-1"


def test_migrate_creates_backup_file(src_db: Path, tmp_path: Path):
    dst = tmp_path / "dst.cozo.db"
    out = migrate(src_db, dst, progress=False)
    bak = Path(out["backup"])
    assert bak.exists()
    assert bak.stat().st_size > 0


def test_migrate_is_re_runnable_idempotent(src_db: Path, tmp_path: Path):
    """2 回 migrate しても relation 件数は同じ (key で upsert)."""
    dst = tmp_path / "dst.cozo.db"
    migrate(src_db, dst, progress=False)
    client = cozo_init(dst)
    n1 = len(client.run("?[id] := *fact{id}")["rows"])
    e1 = len(client.run("?[id] := *episode{id}")["rows"])
    migrate(src_db, dst, progress=False)
    client2 = cozo_init(dst)
    n2 = len(client2.run("?[id] := *fact{id}")["rows"])
    e2 = len(client2.run("?[id] := *episode{id}")["rows"])
    assert n1 == n2 and e1 == e2
