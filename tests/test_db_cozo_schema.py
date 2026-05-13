"""Phase 2.1: Cozo schema integrity tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db_cozo.connection import (
    EMBEDDING_DIM,
    SCHEMA_VERSION,
    existing_hnsw,
    existing_relations,
    init_db,
    next_id,
    set_max_id,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "p.cozo.db"


def test_init_creates_all_relations(db_path: Path):
    client = init_db(db_path)
    rels = existing_relations(client)
    for expected in (
        "fact", "episode", "discussion_node", "discussion_edge",
        "topic", "topic_tag", "topic_relation",
        "meta", "id_seq", "conflict", "lint_log", "recall_trigger",
    ):
        assert expected in rels, f"missing relation: {expected}"


def test_init_creates_hnsw_indexes(db_path: Path):
    client = init_db(db_path)
    for relation in ("fact", "episode", "discussion_node", "topic_tag", "recall_trigger"):
        idxs = existing_hnsw(client, relation)
        assert "vec_idx" in idxs, f"missing HNSW index on {relation}"


def test_init_is_idempotent(db_path: Path):
    """2 回 init しても落ちず、 同じ relation 群を返す."""
    init_db(db_path)
    client2 = init_db(db_path)
    rels = existing_relations(client2)
    assert "fact" in rels


def test_meta_records_schema_version(db_path: Path):
    client = init_db(db_path)
    res = client.run("?[v] := *meta{key: 'schema_version', value: v}")
    assert res["rows"][0][0] == SCHEMA_VERSION


def test_meta_records_embedding_dim(db_path: Path):
    client = init_db(db_path)
    res = client.run("?[v] := *meta{key: 'embedding_dim', value: v}")
    assert int(res["rows"][0][0]) == EMBEDDING_DIM


def test_next_id_increments(db_path: Path):
    client = init_db(db_path)
    seq = [next_id(client, "episode") for _ in range(5)]
    assert seq == [1, 2, 3, 4, 5]
    # 別 entity は独立
    assert next_id(client, "fact") == 1


def test_set_max_id_resets_sequence(db_path: Path):
    client = init_db(db_path)
    set_max_id(client, "episode", 100)
    assert next_id(client, "episode") == 101


def test_can_insert_and_query_fact_with_embedding(db_path: Path):
    client = init_db(db_path)
    fid = next_id(client, "fact")
    embedding = [0.0] * EMBEDDING_DIM
    embedding[5] = 1.0
    client.run(
        "?[id, category, key, value, importance, created_at, updated_at, embedding] "
        "<- [[$id, 'preference', 'coffee', '深煎り', 6, '2026-05-13', '2026-05-13', vec($e)]] "
        ":put fact {id => category, key, value, importance, created_at, updated_at, embedding}",
        {"id": fid, "e": embedding},
    )
    res = client.run("?[id, value] := *fact{id, value}")
    assert res["rows"] == [[fid, "深煎り"]]


def test_hnsw_search_returns_nearest(db_path: Path):
    client = init_db(db_path)
    embeddings = []
    for i, label in enumerate(["A", "B", "C"]):
        e = [0.0] * EMBEDDING_DIM
        e[i] = 1.0
        embeddings.append((next_id(client, "fact"), label, e))
    for fid, label, e in embeddings:
        client.run(
            "?[id, category, key, value, importance, created_at, updated_at, embedding] "
            "<- [[$id, 'k', $k, $v, 5, 't', 't', vec($e)]] "
            ":put fact {id => category, key, value, importance, created_at, updated_at, embedding}",
            {"id": fid, "k": label, "v": label, "e": e},
        )
    # query = A 方向
    q = [0.0] * EMBEDDING_DIM
    q[0] = 1.0
    res = client.run(
        "?[dist, value] := ~fact:vec_idx{value | query: vec($q), k: 3, ef: 50, "
        "bind_distance: dist} :order dist",
        {"q": q},
    )
    rows = res["rows"]
    assert rows[0][1] == "A"
    assert rows[0][0] == pytest.approx(0.0, abs=1e-4)
