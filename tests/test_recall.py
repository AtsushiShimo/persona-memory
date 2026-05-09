"""Phase 4: recall LLM 経路 (extract / search / format / run) のテスト。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode
from scripts.recall.extract import build_prompt, extract_query_keywords, parse_keywords
from scripts.recall.format import to_additional_context
from scripts.recall.run import recall
from scripts.recall.search import bump_access_counts, search
from scripts.write.extract import FactCandidate
from scripts.write.persist import insert_new


@dataclass
class FakeRecallClient:
    """LLM mock. analyze_query と summarize_recall の両方の呼び出しに応答する.

    プロンプトに 'search_history' が含まれていれば analyze 呼び出しと判断、
    それ以外は summarize 呼び出しと判断 (= summary 文字列を返す)。
    """
    keywords: list[str]
    search_history: bool = False
    summary: str = "深煎り好き"  # summarize_recall が返す要約文
    embedding_map: dict[str, list[float]] = field(default_factory=dict)
    # 新仕様: recall は発話全文を embed する。テストでは map に発話文字列を
    # 入れていなくても fact 側の vector と一致させたいので、default を
    # [1.0]+[0]*767 にする (fact 登録側も同じ vector を使う)。
    default_embedding: list[float] = field(default_factory=lambda: [1.0] + [0.0] * 767)

    def generate(self, model: str, prompt: str) -> str:
        if "search_history" in prompt:
            return json.dumps(
                {"keywords": self.keywords, "search_history": self.search_history},
                ensure_ascii=False,
            )
        return self.summary

    def embed(self, model: str, text: str) -> list[float]:
        return list(self.embedding_map.get(text, self.default_embedding))


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


# ── extract ─────────────────────────────────────────────────────────────────

def test_build_prompt_includes_buffer_and_content():
    p = build_prompt("コーヒーは?", [{"role": "user", "content": "好きな飲み物の話"}])
    assert "コーヒーは?" in p
    assert "好きな飲み物の話" in p


def test_parse_keywords_valid():
    assert parse_keywords('{"keywords": ["コーヒー","嗜好"], "search_history": false}') == ["コーヒー", "嗜好"]


def test_parse_keywords_with_code_fence():
    assert parse_keywords('```json\n{"keywords": ["go"], "search_history": false}\n```') == ["go"]


def test_parse_keywords_empty_for_short_utterance():
    assert parse_keywords("[]") == []


def test_parse_keywords_garbage():
    assert parse_keywords("not json") == []
    assert parse_keywords("") == []


def test_extract_query_keywords_with_fake_client():
    client = FakeRecallClient(keywords=["コーヒー"])
    r = extract_query_keywords("深煎り好き?", [], client)
    assert r == ["コーヒー"]


# ── search ──────────────────────────────────────────────────────────────────

def test_search_returns_empty_for_no_facts(db):
    r = search(db, [[1.0] + [0.0] * 767])
    assert r == []


def test_search_finds_active_facts_only(db):
    insert_new(db, FactCandidate("preference", "coffee", "深煎り", 6), [1.0] + [0.0] * 767)
    insert_new(db, FactCandidate("preference", "old_coffee", "ミルク", 6), [1.0] + [0.0] * 767)
    db.execute("UPDATE facts SET status='superseded' WHERE key='old_coffee'")
    db.commit()

    hits = search(db, [[1.0] + [0.0] * 767])
    assert len(hits) == 1
    assert hits[0].key == "coffee"


def test_search_dedupes_across_keywords(db):
    insert_new(db, FactCandidate("preference", "coffee", "深煎り", 6), [1.0] + [0.0] * 767)
    db.commit()
    # 同じ embedding を複数回 keyword 検索しても重複しない
    hits = search(db, [[1.0] + [0.0] * 767, [1.0] + [0.0] * 767])
    assert len(hits) == 1


def test_search_ranks_by_score(db):
    """importance が高い & access_count が多い fact が上位に来る。"""
    insert_new(db, FactCandidate("preference", "low", "v", 3), [1.0] + [0.0] * 767)
    insert_new(db, FactCandidate("preference", "high", "v", 9), [1.0] + [0.0] * 767)
    db.execute("UPDATE facts SET access_count = 10 WHERE key='high'")
    db.commit()

    hits = search(db, [[1.0] + [0.0] * 767])
    assert len(hits) == 2
    assert hits[0].key == "high"  # 高 importance + 高 access_count


def test_search_recalls_fresh_fact_with_zero_access_count(db):
    """access_count=0 の新規 fact も recall に出る (cold-start catch-22 回避)。

    旧式: acc = log(1+0)/5 = 0 → score = 0 → 永久に呼ばれない
    新式: acc = 0.5 + 0.5 × ... → 0.5 ベースで残る
    """
    insert_new(db, FactCandidate("preference", "fresh", "新規 fact", 6), [1.0] + [0.0] * 767)
    db.commit()
    # 全 fact が access_count=0 のはず
    n = db.execute("SELECT COUNT(*) FROM facts WHERE access_count = 0").fetchone()[0]
    assert n == 1

    hits = search(db, [[1.0] + [0.0] * 767])
    assert len(hits) == 1
    assert hits[0].key == "fresh"
    assert hits[0].score > 0  # 旧式バグでは 0 だった


def test_search_excludes_boot_layer(db):
    """boot 層 (persona/rule) は SessionStart で既に注入されてるので recall に出さない。"""
    insert_new(db, FactCandidate("persona", "style", "率直に", 9), [1.0] + [0.0] * 767)
    insert_new(db, FactCandidate("rule", "branch", "main 直 push 不可", 9), [1.0] + [0.0] * 767)
    insert_new(db, FactCandidate("preference", "coffee", "深煎り", 6), [1.0] + [0.0] * 767)
    insert_new(db, FactCandidate("aversion", "sweets", "甘い物控える", 7), [1.0] + [0.0] * 767)
    db.commit()

    hits = search(db, [[1.0] + [0.0] * 767])
    cats = {h.category for h in hits}
    assert "persona" not in cats
    assert "rule" not in cats
    assert "preference" in cats
    assert "aversion" in cats


def test_bump_access_counts(db):
    insert_new(db, FactCandidate("skill", "go", "10y", 7), [1.0] + [0.0] * 767)
    db.commit()
    fid = db.execute("SELECT id FROM facts").fetchone()[0]
    bump_access_counts(db, [fid])
    row = db.execute("SELECT access_count, last_accessed_at FROM facts WHERE id=?", (fid,)).fetchone()
    assert row[0] == 1
    assert row[1] is not None


# ── format ──────────────────────────────────────────────────────────────────

def test_format_empty():
    assert to_additional_context([]) == ""


def test_format_includes_category_key_value():
    from scripts.recall.search import RecalledFact
    facts = [RecalledFact(
        fact_id=1, category="preference", key="coffee", value="深煎り",
        importance=6, access_count=2, distance=0.1, score=0.5,
    )]
    md = to_additional_context(facts)
    assert "## 関連する記憶" in md
    assert "[preference/coffee]" in md
    assert "深煎り" in md


# ── run.recall ──────────────────────────────────────────────────────────────

def test_recall_full_path(db):
    """fact が引かれて、LLM 要約された additionalContext が返る。"""
    save_episode(db, role="user", content="コーヒーの話したい", session_id="s1")
    insert_new(db, FactCandidate("preference", "coffee", "深煎り好き", 6), [1.0] + [0.0] * 767)
    db.commit()

    client = FakeRecallClient(
        keywords=["コーヒー"],
        summary="マスターは深煎りのコーヒーを好んでいる。",
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    out = recall(db, "深煎りまた飲みたい", client)
    assert "## 思い出した記憶" in out
    assert "深煎り" in out
    # 構造化記法 ([category/key]) は出力に含まれない (LLM が自然文に圧縮)
    assert "[preference/coffee]" not in out


def test_recall_empty_when_summary_says_no_match(db):
    """LLM 要約が EMPTY_MARKER を返したら additionalContext は空。"""
    insert_new(db, FactCandidate("preference", "coffee", "深煎り", 6), [1.0] + [0.0] * 767)
    db.commit()
    client = FakeRecallClient(
        keywords=["コーヒー"],
        summary="(該当なし)",
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    assert recall(db, "深煎り好き?", client) == ""


def test_recall_includes_playbook_persona_facts(db):
    """persona/playbook_* は SessionStart 除外だが dynamic recall では hit する.

    `category NOT IN ('persona','rule')` の従来除外を override して
    `OR key LIKE 'playbook_%'` を許可する (= 0.5.19 で追加).
    """
    from scripts.recall.search import search

    insert_new(
        db,
        FactCandidate(
            "persona", "playbook_external_tool_failure",
            "トリガー: Stitch / Figma の接続失敗 / 行動: 不具合検索",
            8,
        ),
        [1.0] + [0.0] * 767,
    )
    # 普通の persona (= boot 層) は recall 除外されることを確認するため一緒に入れる
    insert_new(
        db, FactCandidate("persona", "style", "率直に", 9),
        [1.0] + [0.0] * 767,
    )
    db.commit()

    hits = search(db, [[1.0] + [0.0] * 767])
    keys = [h.key for h in hits]
    assert "playbook_external_tool_failure" in keys
    assert "style" not in keys  # 通常の persona は除外


def test_recall_runs_even_when_keywords_empty(db):
    """keywords=[] (= 短い相槌・合意) でも recall は走る。

    「OK」「うん」「ありがとう」 等は意味の無いやり取りではなく、直前の議題への
    応答なので、過去記憶を踏まえて返答すべき。recall は発話全文を embed して
    検索 → summarize LLM が curate する設計 (空 keywords でも skip しない)。
    """
    insert_new(db, FactCandidate("preference", "coffee", "深煎り", 6), [1.0] + [0.0] * 767)
    db.commit()
    client = FakeRecallClient(keywords=[], summary="深煎り好き")
    out = recall(db, "OK", client)
    # recall は走り、summarize 結果を返す (skip しない)
    assert "## 思い出した記憶" in out
    assert "深煎り" in out


def test_recall_empty_when_no_hits(db):
    """fact 0 件、または全て distance 超なら空。"""
    client = FakeRecallClient(
        keywords=["コーヒー"],
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    assert recall(db, "深煎り", client) == ""
