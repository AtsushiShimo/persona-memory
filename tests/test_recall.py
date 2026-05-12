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
    # 0.6.12: 想起トリガー学習用. None なら trigger_phrase を出さない (後方互換).
    trigger_phrase: str | None = None

    def generate(self, model: str, prompt: str) -> str:
        if "search_history" in prompt:
            return json.dumps(
                {
                    "keywords": self.keywords,
                    "search_history": self.search_history,
                    "trigger_phrase": self.trigger_phrase,
                },
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

    # 0.6.7: 同一 embedding の異なる fact は near-duplicate cluster に潰れるため
    # ランキング検証目的では cluster_enabled=False で従来挙動を維持.
    hits = search(db, [[1.0] + [0.0] * 767], cluster_enabled=False)
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

    # 0.6.7: cluster_enabled=False で boot 層除外のみ純粋検証 (同 embedding
    # の preference/aversion が cluster で 1 件に潰れるのを回避).
    hits = search(db, [[1.0] + [0.0] * 767], cluster_enabled=False)
    cats = {h.category for h in hits}
    assert "persona" not in cats
    assert "rule" not in cats
    assert "preference" in cats
    assert "aversion" in cats


def test_search_clusters_near_duplicates(db):
    """0.6.7: 同一に近い embedding を持つ複数 fact は cluster で 1 件に潰す.

    ローカル LLM の prompt で「同主旨を重複 fact 化しない」 と書いても
    守られないケース (例: 「トークン消費激しい」 系 meta 議論の重複) を
    recall 側で物理的に dedup する.
    """
    # ほぼ同じ embedding (cosine 距離 < 0.15)
    emb1 = [1.0, 0.05] + [0.0] * 766
    emb2 = [1.0, 0.06] + [0.0] * 766  # 1 と極めて近い
    emb3 = [0.0, 0.0, 1.0] + [0.0] * 765  # 明確に直交 (距離 = 1)

    # importance 違いで 3 件挿入 → cluster で 2 残るはず (emb1/emb2 が collapse)
    insert_new(db, FactCandidate("context", "topic_a_v1", "話題A 言及1", 6), emb1)
    insert_new(db, FactCandidate("context", "topic_a_v2", "話題A 言及2", 8), emb2)
    insert_new(db, FactCandidate("context", "topic_b", "別話題", 6), emb3)
    db.commit()

    # 両方の話題に近い query を別々に流す (DISTANCE_MAX で topic_b が
    # 漏れないように emb3 もキーワードに含める)
    hits = search(db, [emb1, emb3])
    keys = [h.key for h in hits]
    # emb1/emb2 cluster からは importance 高い topic_a_v2 が残る
    assert "topic_a_v2" in keys
    # topic_a_v1 (cluster 内で score 低) は dedup される
    assert "topic_a_v1" not in keys
    # 別 cluster の topic_b は残る
    assert "topic_b" in keys


def test_search_cluster_can_be_disabled(db):
    """cluster_enabled=False で従来挙動 (重複も全部残る)."""
    emb1 = [1.0, 0.05] + [0.0] * 766
    emb2 = [1.0, 0.06] + [0.0] * 766
    insert_new(db, FactCandidate("context", "a", "言及1", 6), emb1)
    insert_new(db, FactCandidate("context", "b", "言及2", 6), emb2)
    db.commit()

    hits = search(db, [emb1], cluster_enabled=False)
    keys = {h.key for h in hits}
    assert keys == {"a", "b"}


def test_search_cluster_preserves_far_apart_facts(db):
    """直交する embedding を持つ別話題の fact は cluster で潰されない."""
    emb1 = [1.0] + [0.0] * 767
    emb2 = [0.0, 1.0] + [0.0] * 766  # 直交 (距離 = 1.0)
    insert_new(db, FactCandidate("context", "topic_x", "X", 6), emb1)
    insert_new(db, FactCandidate("context", "topic_y", "Y", 6), emb2)
    db.commit()

    # emb1 で検索 → 両方 hit (distance_max=0.6 内に emb1 のみのはずだが
    # distance_max を緩めるため両方 hit させる用に embedding を選ぶ)
    hits = search(db, [emb1, emb2])
    keys = {h.key for h in hits}
    assert keys == {"topic_x", "topic_y"}


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


def test_truncate_to_budget_under_budget_returns_unchanged():
    from scripts.recall.run import _truncate_to_budget

    text = "短い文章"
    # 4 char ≒ 1 token, budget 100 なら触らない
    assert _truncate_to_budget(text, 100) == text


def test_truncate_to_budget_drops_tail_lines():
    from scripts.recall.run import _truncate_to_budget

    text = "header\n" + "\n".join([f"- fact {i} " + ("x" * 20) for i in range(20)])
    out = _truncate_to_budget(text, 50)  # 50 tokens ≒ 125 chars
    assert len(out) <= len(text)
    assert out.startswith("header")
    # 末尾の fact 19 は落ちている
    assert "fact 19" not in out


def test_truncate_to_budget_disabled_when_zero():
    from scripts.recall.run import _truncate_to_budget

    text = "x" * 10000
    assert _truncate_to_budget(text, 0) == text


def test_recall_empty_when_no_hits(db):
    """fact 0 件、または全て distance 超なら空。"""
    client = FakeRecallClient(
        keywords=["コーヒー"],
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    assert recall(db, "深煎り", client) == ""


# ── 0.6.12 想起トリガー学習 (Phase A: 蓄積のみ) ─────────────────────────────

def test_parse_analysis_extracts_trigger_phrase():
    """parse_analysis が trigger_phrase フィールドを正しく拾うこと."""
    from scripts.recall.extract import parse_analysis

    a = parse_analysis(
        '{"keywords": ["Phase 1"], "search_history": true, '
        '"trigger_phrase": "Phase 1 の決定"}'
    )
    assert a.trigger_phrase == "Phase 1 の決定"
    assert a.search_history is True


def test_parse_analysis_trigger_phrase_null():
    """null / 文字列 'null' / 欠落いずれも None に正規化."""
    from scripts.recall.extract import parse_analysis

    assert parse_analysis(
        '{"keywords": [], "search_history": false, "trigger_phrase": null}'
    ).trigger_phrase is None
    assert parse_analysis(
        '{"keywords": [], "search_history": false, "trigger_phrase": "null"}'
    ).trigger_phrase is None
    assert parse_analysis(
        '{"keywords": [], "search_history": false}'
    ).trigger_phrase is None


def test_recall_saves_recall_trigger_when_phrase_present(db):
    """trigger_phrase 抽出時、 recall_triggers に正例行が作られること."""
    save_episode(db, role="user", content="さっき何話した?", session_id="s1")
    insert_new(db, FactCandidate("preference", "coffee", "深煎り好き", 6), [1.0] + [0.0] * 767)
    db.commit()

    client = FakeRecallClient(
        keywords=["コーヒー"],
        search_history=True,
        trigger_phrase="さっきの議論",
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    recall(db, "さっき何話したっけ?", client, source_episode_id=1)

    rows = db.execute(
        "SELECT trigger_phrase, hit_fact_ids, source_episode_id FROM recall_triggers"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "さっきの議論"
    assert "1" in rows[0][1] or rows[0][1] != "[]"  # 何らかの fact_id が入っている
    assert rows[0][2] == 1


def test_recall_no_trigger_save_when_phrase_absent(db):
    """trigger_phrase が None の通常発話では recall_triggers に行を作らない."""
    insert_new(db, FactCandidate("preference", "coffee", "深煎り好き", 6), [1.0] + [0.0] * 767)
    db.commit()

    client = FakeRecallClient(
        keywords=["コーヒー"],
        search_history=False,
        trigger_phrase=None,  # 過去参照なし
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    recall(db, "コーヒーの話", client, source_episode_id=1)

    n = db.execute("SELECT COUNT(*) FROM recall_triggers").fetchone()[0]
    assert n == 0
