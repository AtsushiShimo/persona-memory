"""Phase 1: schema integrity tests."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.db.connection import EMBEDDING_DIM, SCHEMA_VERSION, connect
from scripts.db.migrate import init_db


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


def test_tables_exist(db: sqlite3.Connection):
    rows = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    for expected in ("facts", "episodes", "escalation_log", "meta", "fact_embeddings", "episode_embeddings"):
        assert expected in rows, f"missing table: {expected}"


def test_meta_records_schema_version(db: sqlite3.Connection):
    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert row[0] == SCHEMA_VERSION
    row = db.execute("SELECT value FROM meta WHERE key='embedding_dim'").fetchone()
    assert int(row[0]) == EMBEDDING_DIM


def test_facts_unique_active_constraint(db: sqlite3.Connection):
    """active 層は (category, key) で一意。superseded は重複 OK。"""
    db.execute(
        "INSERT INTO facts(category, key, value, importance) VALUES ('preference','coffee','dark roast',6)"
    )
    # 同じ (category, key) で active を 2 つ目入れると失敗
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO facts(category, key, value, importance) VALUES ('preference','coffee','light roast',6)"
        )

    # 旧版を superseded に降格すれば、新 active を入れられる
    db.execute("UPDATE facts SET status='superseded' WHERE category='preference' AND key='coffee'")
    db.execute(
        "INSERT INTO facts(category, key, value, importance) VALUES ('preference','coffee','light roast',6)"
    )

    # superseded 同士は重複できる
    db.execute(
        "INSERT INTO facts(category, key, value, importance, status) VALUES ('preference','coffee','medium roast',6,'superseded')"
    )

    db.commit()
    counts = db.execute(
        "SELECT status, COUNT(*) FROM facts WHERE category='preference' AND key='coffee' GROUP BY status"
    ).fetchall()
    counts_dict = dict(counts)
    assert counts_dict["active"] == 1
    assert counts_dict["superseded"] == 2


def test_upsert_fact_via_server_db(tmp_path: Path, monkeypatch):
    """MCP `write_fact` 経由 (server/db.upsert_fact) の ON CONFLICT が
    partial unique index と整合することを担保する回帰テスト。
    過去に `ON CONFLICT(category, key)` だけだと partial index にマッチせず
    OperationalError で落ちた."""
    db_path = tmp_path / "upsert.db"
    init_db(db_path)
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db_path))
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "1")
    from server import db as server_db

    fid1 = server_db.upsert_fact(
        category="preference", key="coffee", value="dark roast",
        importance=6, source="t1",
    )
    fid2 = server_db.upsert_fact(
        category="preference", key="coffee", value="light roast",
        importance=7, source="t2",
    )
    assert fid1 == fid2, "same (category, key) should UPDATE, not INSERT"

    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT value, importance, source FROM facts "
            "WHERE category='preference' AND key='coffee'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["value"] == "light roast"
        assert rows[0]["importance"] == 7
    finally:
        conn.close()


def test_facts_category_check(db: sqlite3.Connection):
    # 'unknown' は CHECK 制約に含まれないので必ず弾かれる.
    # ('knowledge' は 0.5.25 以降 valid なので、 invalid 例には使えない).
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO facts(category, key, value, importance) VALUES ('unknown','x','y',5)"
        )


def test_facts_importance_check(db: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO facts(category, key, value, importance) VALUES ('persona','x','y',10)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO facts(category, key, value, importance) VALUES ('persona','x','y',0)"
        )


def test_facts_supersedes_fk(db: sqlite3.Connection):
    """supersedes / superseded_by は facts(id) を参照。存在しない id は弾かれる。"""
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO facts(category, key, value, importance, supersedes) "
            "VALUES ('persona','x','y',5,9999)"
        )


def test_episodes_role_check(db: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO episodes(role, content, session_id) VALUES ('system','hi','s1')"
        )


def test_escalation_log_constraints(db: sqlite3.Connection):
    db.execute(
        "INSERT INTO escalation_log(reason, caller, input_size) VALUES ('long_input','write',2500)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO escalation_log(reason, caller) VALUES ('something_else','write')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO escalation_log(reason, caller) VALUES ('uncertainty','main')"
        )


def test_vec_tables_accept_embeddings(db: sqlite3.Connection):
    """fact_embeddings / episode_embeddings は INSERT 可能。"""
    import struct

    db.execute(
        "INSERT INTO facts(category, key, value, importance) VALUES ('skill','python',10,5)"
    )
    fact_id = db.execute("SELECT id FROM facts").fetchone()[0]
    blob = struct.pack(f"{EMBEDDING_DIM}f", *([0.0] * EMBEDDING_DIM))
    db.execute("INSERT INTO fact_embeddings(fact_id, embedding) VALUES (?, ?)", (fact_id, blob))
    db.commit()
    row = db.execute("SELECT fact_id FROM fact_embeddings").fetchone()
    assert row[0] == fact_id


def test_vec_tables_use_cosine_distance(db: sqlite3.Connection):
    """vec0 は cosine 距離で動く (L2 ではない)。スケール無視で方向のみで判断。"""
    import struct

    # facts に 3 件、別スケールの 'x 軸方向' / '直交' / '真逆' を入れる
    rows = []
    for key, vec in [
        ("unit_x", [1.0] + [0.0] * (EMBEDDING_DIM - 1)),
        ("scaled_x", [10.0] + [0.0] * (EMBEDDING_DIM - 1)),  # 同方向、スケール 10x
        ("orthogonal", [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)),  # 直交
        ("opposite", [-1.0] + [0.0] * (EMBEDDING_DIM - 1)),  # 真逆
    ]:
        cur = db.execute(
            "INSERT INTO facts(category, key, value, importance) VALUES ('skill', ?, 'v', 5)",
            (key,),
        )
        fid = cur.lastrowid
        blob = struct.pack(f"{EMBEDDING_DIM}f", *vec)
        db.execute(
            "INSERT INTO fact_embeddings(fact_id, embedding) VALUES (?, ?)",
            (fid, blob),
        )
        rows.append((key, fid))
    db.commit()

    # query = 単位 x 軸
    q = struct.pack(f"{EMBEDDING_DIM}f", *([1.0] + [0.0] * (EMBEDDING_DIM - 1)))
    hits = db.execute("""
        SELECT facts.key, fact_embeddings.distance
        FROM fact_embeddings
        JOIN facts ON facts.id = fact_embeddings.fact_id
        WHERE fact_embeddings.embedding MATCH ? AND k = 4
        ORDER BY fact_embeddings.distance
    """, (q,)).fetchall()
    by_key = {h[0]: h[1] for h in hits}

    # cosine 距離: 同方向は 0、スケール違いも 0、直交 1、真逆 2
    assert abs(by_key["unit_x"] - 0.0) < 1e-4
    assert abs(by_key["scaled_x"] - 0.0) < 1e-4, f"L2 ならここで非 0、cosine なら 0: {by_key['scaled_x']}"
    assert abs(by_key["orthogonal"] - 1.0) < 1e-4
    assert abs(by_key["opposite"] - 2.0) < 1e-4
