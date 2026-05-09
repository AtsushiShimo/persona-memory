"""Phase 3: write LLM 経路 (extract / similarity / persist / run) のテスト。

Ollama は呼ばない — FakeClient (LLMClient duck-type) で固定出力。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode
from scripts.write.extract import (
    FactCandidate,
    build_prompt,
    extract_facts,
    parse_response,
)
from scripts.write.persist import apply_candidate, insert_new
from scripts.write.run import process_episode
from scripts.write.similarity import (
    Match,
    find_by_embedding,
    find_by_key,
    find_match,
    is_reinforcement,
)


# ── Fake LLM client ──────────────────────────────────────────────────────────

@dataclass
class FakeClient:
    facts: list[dict]
    embed_value: list[float] = None  # type: ignore

    def __post_init__(self):
        if self.embed_value is None:
            self.embed_value = [0.0] * 768

    def generate(self, model: str, prompt: str) -> str:
        return json.dumps(self.facts, ensure_ascii=False)

    def embed(self, model: str, text: str) -> list[float]:
        return list(self.embed_value)


# ── extract ─────────────────────────────────────────────────────────────────

def test_build_prompt_includes_buffer_and_role():
    p = build_prompt(
        role="user",
        content="深煎りが好き",
        buffer=[{"role": "user", "content": "コーヒーの話"}],
    )
    assert "深煎り" in p
    assert "コーヒーの話" in p
    assert "(user)" in p


def test_parse_response_valid():
    text = json.dumps([
        {"category": "preference", "key": "coffee", "value": "深煎り", "importance": 6}
    ])
    r = parse_response(text)
    assert len(r) == 1
    assert r[0].category == "preference"
    assert r[0].key == "coffee"
    assert r[0].importance == 6


def test_parse_response_with_code_fence():
    text = "```json\n[{\"category\":\"skill\",\"key\":\"go\",\"value\":\"10y exp\",\"importance\":7}]\n```"
    r = parse_response(text)
    assert len(r) == 1
    assert r[0].category == "skill"


def test_parse_response_invalid_category_dropped():
    text = json.dumps([
        {"category": "knowledge", "key": "x", "value": "y", "importance": 5},
        {"category": "skill", "key": "k", "value": "v", "importance": 5},
    ])
    r = parse_response(text)
    assert len(r) == 1
    assert r[0].category == "skill"


def test_parse_response_garbage():
    assert parse_response("") == []
    assert parse_response("not json at all") == []
    assert parse_response("{}") == []  # 配列でない


def test_extract_facts_with_fake_client():
    client = FakeClient(facts=[
        {"category": "preference", "key": "coffee", "value": "深煎り", "importance": 6}
    ])
    r = extract_facts(role="user", content="深煎り好き", buffer=[], client=client)
    assert len(r) == 1
    assert r[0].key == "coffee"


def test_extract_facts_via_claude_backend(monkeypatch):
    """backend='claude' で _extract_via_claude_backend (= invoke_claude) が
    呼ばれ、ollama client.generate は呼ばれないこと.
    """
    import json as _json
    from dataclasses import dataclass

    @dataclass
    class _Result:
        text: str
        success: bool
        error: str = ""

    fake_response = _json.dumps([
        {"category": "preference", "key": "coffee", "value": "浅煎り", "importance": 6}
    ], ensure_ascii=False)

    calls = {"invoke_claude": 0, "ollama_generate": 0}

    def fake_invoke(prompt, timeout=120):
        calls["invoke_claude"] += 1
        return _Result(text=fake_response, success=True)

    monkeypatch.setattr(
        "scripts.escalate.claude_p.invoke_claude", fake_invoke,
    )

    class _GuardedClient:
        def generate(self, model, prompt):
            calls["ollama_generate"] += 1
            return "[]"
        def embed(self, model, text):
            return [1.0] + [0.0] * 767

    r = extract_facts(
        role="user", content="浅煎り好き", buffer=[],
        client=_GuardedClient(), backend="claude",
    )
    assert calls["invoke_claude"] == 1
    assert calls["ollama_generate"] == 0
    assert len(r) == 1
    assert r[0].value == "浅煎り"


def test_extract_facts_default_backend_is_ollama():
    """backend 指定なし (default) は ollama (= client.generate) を使う."""
    client = FakeClient(facts=[
        {"category": "preference", "key": "coffee", "value": "深煎り", "importance": 6}
    ])
    r = extract_facts(role="user", content="x", buffer=[], client=client)
    # FakeClient.generate が呼ばれて facts が返ってきていること
    assert len(r) == 1


def test_is_same_attribute_treats_last_word_as_attribute():
    """末尾の単語が同じ key 同士は同属性、違えば別属性 (0.5.17 追加)."""
    from scripts.write.similarity import _is_same_attribute

    # 同末尾 = 同属性
    assert _is_same_attribute("coffee_preference", "coffee_taste_preference")
    assert _is_same_attribute("a_name", "b_name")
    assert _is_same_attribute("name", "name")

    # 異末尾 = 別属性
    assert not _is_same_attribute("pet_dog_name", "pet_dog_breed")
    assert not _is_same_attribute("pet_dog_name", "pet_dog_gender")
    assert not _is_same_attribute("coffee_roast", "coffee_sugar")

    # 大小文字無視
    assert _is_same_attribute("foo_NAME", "bar_name")


def test_is_same_attribute_strips_dup_suffix():
    """seen_keys 由来の `_2`/`_3` suffix を剥がしてから末尾比較する (0.5.18)."""
    from scripts.write.similarity import _is_same_attribute, _strip_dup_suffix

    # suffix 剥がし
    assert _strip_dup_suffix("coffee_milk_2") == "coffee_milk"
    assert _strip_dup_suffix("coffee_milk_42") == "coffee_milk"
    assert _strip_dup_suffix("coffee_milk") == "coffee_milk"  # 変更なし

    # suffix 違いでも同属性扱い
    assert _is_same_attribute("coffee_milk", "coffee_milk_2")
    assert _is_same_attribute("coffee_milk_2", "coffee_milk_3")
    # 異属性は依然別物
    assert not _is_same_attribute("coffee_milk_2", "coffee_roast")


# ── similarity / persist ─────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


def test_find_by_key_returns_active_only(db):
    db.execute(
        "INSERT INTO facts(category, key, value, importance) VALUES "
        "('preference','coffee','dark',5)"
    )
    db.commit()
    m = find_by_key(db, "preference", "coffee")
    assert m is not None
    assert m.value == "dark"
    assert m.method == "key"

    db.execute("UPDATE facts SET status='superseded'")
    db.commit()
    assert find_by_key(db, "preference", "coffee") is None


def test_is_reinforcement():
    assert is_reinforcement("深煎り好き", "深煎り好き") is True
    assert is_reinforcement("深煎り", "深煎り好き") is True  # 部分文字列
    assert is_reinforcement("深煎り好き", "ミルクと砂糖たっぷり") is False
    assert is_reinforcement("", "x") is False


def test_apply_candidate_insert(db):
    cand = FactCandidate(category="skill", key="python", value="10y exp", importance=7)
    action = apply_candidate(db, cand, match=None, embedding=[0.1] * 768, source="conversation")
    assert action == "insert"

    rows = db.execute("SELECT category, key, value, status FROM facts").fetchall()
    assert rows == [("skill", "python", "10y exp", "active")]


def test_apply_candidate_reinforce(db):
    insert_new(db, FactCandidate("preference", "coffee", "深煎り", 5), [0.1] * 768)
    db.commit()

    match = find_by_key(db, "preference", "coffee")
    new_cand = FactCandidate("preference", "coffee", "深煎りが好き", 6)
    action = apply_candidate(db, new_cand, match, embedding=[0.1] * 768)
    assert action == "reinforce"

    row = db.execute(
        "SELECT value, importance, access_count, status FROM facts WHERE category='preference' AND key='coffee'"
    ).fetchone()
    assert row[0] == "深煎りが好き"  # value 更新
    assert row[1] >= 6  # importance 加算
    assert row[2] == 1  # access_count 加算
    assert row[3] == "active"


def test_apply_candidate_supersede(db):
    insert_new(db, FactCandidate("preference", "coffee", "深煎り", 5), [0.1] * 768)
    db.commit()

    match = find_by_key(db, "preference", "coffee")
    new_cand = FactCandidate("preference", "coffee", "ミルクたっぷりが好き", 6)
    action = apply_candidate(db, new_cand, match, embedding=[0.2] * 768)
    assert action == "supersede"

    rows = db.execute(
        "SELECT value, status, supersedes, superseded_by FROM facts "
        "WHERE category='preference' AND key='coffee' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    old, new = rows
    assert old[1] == "superseded"
    assert old[3] is not None  # superseded_by points to new
    assert new[1] == "active"
    assert new[2] == 1  # supersedes points to old (id=1)
    assert new[0] == "ミルクたっぷりが好き"


def test_unique_active_constraint_enforced_during_supersede(db):
    """旧 active を superseded に降格する前に新 active を入れると UNIQUE 違反になる。
    apply_candidate は降格 → 挿入の順なので守られる。"""
    insert_new(db, FactCandidate("preference", "coffee", "old", 5), [0.1] * 768)
    db.commit()
    match = find_by_key(db, "preference", "coffee")
    new_cand = FactCandidate("preference", "coffee", "完全に違う値", 6)
    apply_candidate(db, new_cand, match, embedding=[0.2] * 768)
    # active は 1 件のみ
    n = db.execute(
        "SELECT COUNT(*) FROM facts WHERE category='preference' AND key='coffee' AND status='active'"
    ).fetchone()[0]
    assert n == 1


def test_find_by_embedding_returns_nearby_match(db):
    """同 category 内で近い embedding を持つ active fact が引ける。"""
    # 異なる key で同概念 (embedding が近い) の fact を入れる
    insert_new(db, FactCandidate("preference", "drink_coffee", "深煎り好き", 5), [1.0] + [0.0] * 767)
    db.commit()

    # 別 key だが embedding が同じ → 近傍ヒットする想定
    m = find_by_embedding(db, "preference", [1.0] + [0.0] * 767, distance_max=0.5)
    assert m is not None
    assert m.method == "embedding"
    assert m.key == "drink_coffee"


def test_find_match_prefers_key(db):
    """key 一致があれば embedding は使わない。"""
    insert_new(db, FactCandidate("skill", "go", "10y", 7), [1.0] + [0.0] * 767)
    db.commit()
    # 別の embedding を渡しても key 一致が勝つ
    m = find_match(db, "skill", "go", [0.0, 1.0] + [0.0] * 766)
    assert m is not None
    assert m.method == "key"


# ── run.process_episode ──────────────────────────────────────────────────────

def test_process_episode_inserts_new_fact(db):
    eid = save_episode(db, role="user", content="コーヒーは深煎りが好き", session_id="s1")
    client = FakeClient(facts=[
        {"category": "preference", "key": "coffee", "value": "深煎り", "importance": 6}
    ])
    results = process_episode(db, eid, buffer_n=3, client=client)
    assert len(results) == 1
    assert results[0][1] == "insert"

    n = db.execute("SELECT COUNT(*) FROM facts WHERE category='preference'").fetchone()[0]
    assert n == 1


def test_process_episode_reinforces_existing(db):
    insert_new(db, FactCandidate("preference", "coffee", "深煎り", 5), [0.1] * 768)
    db.commit()

    eid = save_episode(db, role="user", content="やっぱり深煎り", session_id="s1")
    client = FakeClient(facts=[
        {"category": "preference", "key": "coffee", "value": "深煎り", "importance": 6}
    ])
    results = process_episode(db, eid, buffer_n=3, client=client)
    assert len(results) == 1
    assert results[0][1] == "reinforce"


def test_process_episode_supersedes(db):
    insert_new(db, FactCandidate("preference", "coffee", "深煎り好き", 5), [0.1] * 768)
    db.commit()

    eid = save_episode(db, role="user", content="やっぱりラテが好きになった", session_id="s1")
    client = FakeClient(facts=[
        {"category": "preference", "key": "coffee", "value": "ラテが好き", "importance": 6}
    ], embed_value=[0.9] + [0.0] * 767)  # 違う embedding
    results = process_episode(db, eid, buffer_n=3, client=client)
    assert len(results) == 1
    assert results[0][1] == "supersede"


def test_process_episode_no_facts_extracted(db):
    eid = save_episode(db, role="user", content="OK", session_id="s1")
    client = FakeClient(facts=[])
    results = process_episode(db, eid, buffer_n=3, client=client)
    assert results == []


def test_process_episode_embeds_episode_for_recall(db):
    """process_episode は副次的に episode 全文を embed して
    episode_embeddings に格納する (recall 時のベクトル検索用)."""
    eid = save_episode(db, role="user", content="コーヒー好き", session_id="s1")
    client = FakeClient(facts=[])  # fact 抽出は無くても episode は embed される
    process_episode(db, eid, buffer_n=3, client=client)

    row = db.execute(
        "SELECT episode_id FROM episode_embeddings WHERE episode_id=?",
        (eid,),
    ).fetchone()
    assert row is not None
