"""scripts.db_cozo.lint のテスト.

lint の Cozo 移植が SQLite 版と同等の挙動 (近傍取得 / auto_supersede /
record_conflict / record_lint_run) を見せるかの境界確認.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.fact_persist import (
    fetch_facts_by_ids, insert_new,
)
from scripts.db_cozo.lint import (
    auto_supersede, fetch_fact, fetch_neighbors,
    record_conflict, record_lint_run,
)
from scripts.write.extract import FactCandidate


@pytest.fixture
def client(tmp_path: Path):
    return init_db(tmp_path / "p.cozo.db")


def _cand(cat: str, key: str, val: str, imp: int = 5) -> FactCandidate:
    return FactCandidate(category=cat, key=key, value=val, importance=imp)


def test_fetch_fact_returns_minimal_dict(client):
    fid = insert_new(client, _cand("preference", "coffee", "深煎り"), [0.1] * 768)
    f = fetch_fact(client, fid)
    assert f["category"] == "preference"
    assert f["key"] == "coffee"
    assert f["status"] == "active"


def test_fetch_neighbors_excludes_self_and_different_attribute(client):
    """同 category + 同属性 (key 末尾語一致) の近傍のみ返る."""
    fid1 = insert_new(
        client, _cand("preference", "coffee_preference", "深煎り"),
        [0.1] + [0.0] * 767,
    )
    # 同属性 (末尾 preference): hit する
    fid2 = insert_new(
        client, _cand("preference", "tea_preference", "緑茶"),
        [0.1] + [0.0] * 767,
    )
    # 異属性: 除外
    insert_new(
        client, _cand("preference", "pet_dog_breed", "プードル"),
        [0.1] + [0.0] * 767,
    )
    neighbors = fetch_neighbors(
        client, fid1, "preference", "coffee_preference",
        [0.1] + [0.0] * 767, top_k=5, distance_max=0.6,
    )
    ids = [n["id"] for n in neighbors]
    assert fid2 in ids  # 同属性
    assert fid1 not in ids  # self 除外
    # pet_dog_breed は別属性で除外
    breed_kid = [n for n in neighbors if n["key"].endswith("breed")]
    assert breed_kid == []


def test_auto_supersede_demotes_older_and_links(client):
    """older を superseded 降格 + superseded_by = newer."""
    older = insert_new(client, _cand("preference", "coffee", "深煎り"), [0.1] * 768)
    newer = insert_new(client, _cand("preference", "coffee_2", "浅煎り"), [0.2] * 768)
    auto_supersede(client, older, newer)
    rows = fetch_facts_by_ids(client, [older, newer])
    by_id = {r["id"]: r for r in rows}
    assert by_id[older]["status"] == "superseded"
    assert by_id[older]["superseded_by"] == newer
    assert by_id[newer]["status"] == "active"
    # newer の supersedes が未設定なら older を入れる
    assert by_id[newer]["supersedes"] == older


def test_auto_supersede_keeps_newer_supersedes_if_already_set(client):
    """newer に既に supersedes が入っていれば上書きしない."""
    a = insert_new(client, _cand("preference", "coffee_a", "A"), [0.1] * 768)
    b = insert_new(
        client, _cand("preference", "coffee_b", "B"), [0.2] * 768,
        supersedes=a,
    )
    c = insert_new(client, _cand("preference", "coffee_c", "C"), [0.3] * 768)
    auto_supersede(client, c, b)
    rows = fetch_facts_by_ids(client, [b, c])
    by_id = {r["id"]: r for r in rows}
    # b の supersedes は a のまま (c に上書きされない)
    assert by_id[b]["supersedes"] == a


def test_record_conflict_writes_row(client):
    fid = record_conflict(client, 10, 20, 88, "flagged")
    res = client.run(
        "?[id, fact_a_id, fact_b_id, confidence, resolution] := "
        "*conflict{id, fact_a_id, fact_b_id, confidence, resolution}, "
        "id = $id",
        {"id": fid},
    )
    r = res["rows"][0]
    assert r[1] == 10
    assert r[2] == 20
    assert r[3] == 88
    assert r[4] == "flagged"


def test_record_lint_run_writes_row(client):
    lid = record_lint_run(
        client, pairs_examined=5, flagged=2, auto_resolved=1, trigger="manual",
    )
    res = client.run(
        "?[id, pairs_examined, conflicts_flagged, "
        "conflicts_auto_resolved, trigger_kind] := "
        "*lint_log{id, pairs_examined, conflicts_flagged, "
        "conflicts_auto_resolved, trigger_kind}, id = $id",
        {"id": lid},
    )
    r = res["rows"][0]
    assert r[1] == 5
    assert r[2] == 2
    assert r[3] == 1
    assert r[4] == "manual"
