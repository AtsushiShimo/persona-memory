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


def test_facts_category_check(db: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO facts(category, key, value, importance) VALUES ('knowledge','x','y',5)"
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
