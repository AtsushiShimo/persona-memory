"""scripts.db_cozo.fact_persist のテスト.

write/persist.py の Cozo 版が同等の挙動 (insert / reinforce / supersede /
protected / dirty フラグ) を見せるかの境界確認.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.fact_persist import (
    apply_candidate, clear_boot_dirty, fetch_boot_facts, fetch_facts_by_ids,
    find_match, insert_new, is_boot_dirty, mark_boot_dirty, reinforce,
    supersede,
)
from scripts.write.extract import FactCandidate


@pytest.fixture
def client(tmp_path: Path):
    return init_db(tmp_path / "p.cozo.db")


def _cand(cat: str, key: str, val: str, imp: int = 5) -> FactCandidate:
    return FactCandidate(category=cat, key=key, value=val, importance=imp)


def test_insert_new_returns_id(client):
    fid = insert_new(client, _cand("preference", "coffee", "深煎り"), [0.1] * 768)
    assert fid == 1
    row = fetch_facts_by_ids(client, [fid])[0]
    assert row["category"] == "preference"
    assert row["value"] == "深煎り"
    assert row["status"] == "active"


def test_find_match_by_key(client):
    insert_new(client, _cand("preference", "coffee", "深煎り"), [0.1] * 768)
    m = find_match(client, "preference", "coffee", [0.0] * 768)
    assert m is not None
    assert m.method == "key"
    assert m.fact_id == 1


def test_find_match_by_embedding_same_attribute(client):
    """末尾語が同じ key なら embedding 近傍 hit (= 表記揺れ救済)."""
    insert_new(
        client, _cand("preference", "coffee_preference", "深煎り"),
        [0.1] + [0.0] * 767,
    )
    m = find_match(
        client, "preference", "coffee_taste_preference", [0.1] + [0.0] * 767,
    )
    assert m is not None
    assert m.method == "embedding"


def test_find_match_returns_none_for_different_attribute(client):
    """末尾語が違う key は embedding 近くても別属性扱い."""
    insert_new(
        client, _cand("profile", "pet_dog_name", "まろん"),
        [0.1] + [0.0] * 767,
    )
    m = find_match(client, "profile", "pet_dog_breed", [0.1] + [0.0] * 767)
    assert m is None


def test_apply_candidate_protected_key(client):
    """PROTECTED_KEYS は overwrite されない."""
    # persona/natural_voice は default で protected
    out = apply_candidate(
        client, _cand("persona", "natural_voice", "勝手な書き換え"),
        None, [0.1] * 768,
    )
    assert out == "protected"


def test_apply_candidate_supersede_chain(client):
    """value 変更で supersede chain が作られる."""
    apply_candidate(
        client, _cand("preference", "coffee", "深煎り"),
        None, [0.1] * 768, source="test",
    )
    m = find_match(client, "preference", "coffee", [0.1] * 768)
    out = apply_candidate(
        client, _cand("preference", "coffee", "浅煎り"),
        m, [0.2] * 768, source="test", reason="味変更",
    )
    assert out == "supersede"
    rows = fetch_facts_by_ids(client, [1, 2])
    by_id = {r["id"]: r for r in rows}
    assert by_id[1]["status"] == "superseded"
    assert by_id[1]["superseded_by"] == 2
    assert by_id[1]["reason_superseded"] == "味変更"
    assert by_id[2]["status"] == "active"
    assert by_id[2]["supersedes"] == 1


def test_apply_candidate_reinforce_value_match(client):
    """ほぼ同じ value なら supersede ではなく reinforce."""
    apply_candidate(
        client, _cand("preference", "coffee", "深煎りが好きです"),
        None, [0.1] * 768,
    )
    m = find_match(client, "preference", "coffee", [0.1] * 768)
    out = apply_candidate(
        client, _cand("preference", "coffee", "深煎り"),  # 部分一致
        m, [0.1] * 768,
    )
    assert out == "reinforce"
    rows = fetch_facts_by_ids(client, [1])
    assert rows[0]["status"] == "active"


def test_fetch_boot_facts_only_persona_and_rule(client):
    insert_new(client, _cand("persona", "name", "ミリム", imp=8), None)
    insert_new(client, _cand("rule", "always_test", "テストする", imp=7), None)
    insert_new(client, _cand("preference", "coffee", "深煎り"), None)
    insert_new(client, _cand("persona", "playbook_xxx", "ノウハウ"), None)
    boot = fetch_boot_facts(client)
    keys = {(b["category"], b["key"]) for b in boot}
    assert ("persona", "name") in keys
    assert ("rule", "always_test") in keys
    assert ("preference", "coffee") not in keys
    # playbook_ プレフィクスは除外
    assert ("persona", "playbook_xxx") not in keys


def test_boot_dirty_flag(client):
    assert is_boot_dirty(client) is False
    mark_boot_dirty(client)
    assert is_boot_dirty(client) is True
    clear_boot_dirty(client)
    assert is_boot_dirty(client) is False


def test_apply_candidate_boot_category_marks_dirty(client):
    """boot 層 (persona / rule) に書くと dirty フラグが立つ.

    PROTECTED_KEYS は write LLM からの書込を物理的に拒否するので、
    そこ以外の任意 key で確認する.
    """
    apply_candidate(
        client, _cand("persona", "favorite_food", "ラーメン"),
        None, [0.1] * 768,
    )
    assert is_boot_dirty(client) is True


def test_apply_candidate_non_boot_does_not_mark_dirty(client):
    """preference 等は dirty を立てない."""
    apply_candidate(
        client, _cand("preference", "coffee", "深煎り"),
        None, [0.1] * 768,
    )
    assert is_boot_dirty(client) is False
