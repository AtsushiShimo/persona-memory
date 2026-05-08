"""recall パイプラインに episodes 検索を追加した分のテスト."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode
from scripts.recall.format import to_additional_context
from scripts.recall.run import recall
from scripts.recall.search import (
    RecalledEpisode,
    search_episodes_by_keywords,
)


@dataclass
class FakeRecallClient:
    """analyze_query と summarize_recall 両方に応答する mock."""
    keywords: list[str]
    search_history: bool = False
    summary: str = "深煎りコーヒーの話があった"
    embedding_map: dict[str, list[float]] = field(default_factory=dict)
    default_embedding: list[float] = field(default_factory=lambda: [0.0] * 768)

    def generate(self, model, prompt):
        if "search_history" in prompt:
            return json.dumps(
                {"keywords": self.keywords, "search_history": self.search_history},
                ensure_ascii=False,
            )
        return self.summary

    def embed(self, model, text):
        return list(self.embedding_map.get(text, self.default_embedding))


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


# ── trigger 検出は LLM 判断に委譲したので test_recall.py 側の analyze_query
#    系テストでカバー。ここでは episodes 検索が実際に動くか確認する。

# ── search_episodes_by_keywords ─────────────────────────────────────────────

def test_search_episodes_returns_empty_for_no_keywords(db):
    save_episode(db, "user", "コーヒーの話", "s1")
    assert search_episodes_by_keywords(db, []) == []
    assert search_episodes_by_keywords(db, [""]) == []


def test_search_episodes_finds_matching_content(db):
    save_episode(db, "user", "コーヒーは深煎りが好き", "s1")
    save_episode(db, "assistant", "了解しました", "s1")
    save_episode(db, "user", "Python の話に切り替え", "s1")

    hits = search_episodes_by_keywords(db, ["コーヒー"])
    assert len(hits) == 1
    assert hits[0].role == "user"
    assert "コーヒー" in hits[0].content


def test_search_episodes_multiple_keywords_or(db):
    save_episode(db, "user", "コーヒー好き", "s1")
    save_episode(db, "user", "Python 書いてる", "s1")
    save_episode(db, "user", "天気の話", "s1")

    hits = search_episodes_by_keywords(db, ["コーヒー", "Python"])
    contents = [h.content for h in hits]
    assert any("コーヒー" in c for c in contents)
    assert any("Python" in c for c in contents)
    assert all("天気" not in c for c in contents)


def test_search_episodes_orders_newest_first(db):
    save_episode(db, "user", "古い: コーヒーの話 1", "s1")
    save_episode(db, "user", "新しい: コーヒーの話 2", "s1")

    hits = search_episodes_by_keywords(db, ["コーヒー"])
    # ORDER BY id DESC で最新が先
    assert "新しい" in hits[0].content
    assert "古い" in hits[1].content


def test_search_episodes_dedupes_identical_content(db):
    """連投された同じ質問は重複して返さない (GROUP BY content, role)."""
    save_episode(db, "user", "ペットの名前覚えてる?", "s1")
    save_episode(db, "user", "ペットの名前覚えてる?", "s1")
    save_episode(db, "user", "ペットの名前覚えてる?", "s1")
    save_episode(db, "assistant", "申し訳ありません、思い出せません", "s1")
    save_episode(db, "user", "ペットの名前覚えてる?", "s1")  # まだ重複

    hits = search_episodes_by_keywords(db, ["ペット"])
    # 同じ user 質問は 1 件に潰される、assistant 応答は別 role なので残る
    contents = [(h.role, h.content) for h in hits]
    assert ("user", "ペットの名前覚えてる?") in contents
    assert sum(1 for r, c in contents if c == "ペットの名前覚えてる?") == 1


def test_search_episodes_keeps_distinct_role_for_same_content(db):
    """role が違えば content が同じでも別レコードとして残る."""
    save_episode(db, "user", "OK", "s1")
    save_episode(db, "assistant", "OK", "s1")

    hits = search_episodes_by_keywords(db, ["OK"])
    roles = sorted(h.role for h in hits)
    assert roles == ["assistant", "user"]


def test_search_episodes_truncates_long_content(db):
    long_text = "コーヒー" + "あ" * 1000
    save_episode(db, "user", long_text, "s1")
    hits = search_episodes_by_keywords(db, ["コーヒー"])
    assert len(hits) == 1
    # 切り詰めマーカー付き
    assert hits[0].content.endswith("…")


# ── format with episodes ────────────────────────────────────────────────────

def test_format_includes_episodes_section():
    eps = [RecalledEpisode(
        episode_id=1, role="user", content="コーヒーの話",
        timestamp="2026-05-07 14:00:00",
    )]
    md = to_additional_context([], eps)
    assert "## 関連する過去の会話" in md
    assert "[user]" in md
    assert "コーヒーの話" in md


def test_format_no_section_when_both_empty():
    assert to_additional_context([], []) == ""
    assert to_additional_context([], None) == ""


# ── recall full path with trigger ───────────────────────────────────────────

def test_recall_searches_episodes_when_llm_says_history(db):
    """LLM が search_history=true を返した時に episodes が検索され、要約に反映."""
    save_episode(db, "user", "コーヒーは深煎りが好き", "s1")
    save_episode(db, "user", "履歴を見せて", "s1")
    db.commit()

    client = FakeRecallClient(
        keywords=["コーヒー"],
        search_history=True,
        summary="以前マスターは深煎りコーヒーが好きと話していた。",
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    out = recall(db, "履歴の中でコーヒーの話あった?", client)
    # 出力は要約された自然文 (構造化記法ではない)
    assert "## 思い出した記憶" in out
    assert "深煎り" in out
    # 旧形式のセクション見出しは無くなる (要約に統合される)
    assert "## 関連する過去の会話" not in out


def test_recall_returns_summary_only_when_llm_filters_all(db):
    """LLM が「全て無関係」 と判定したら additionalContext は空."""
    save_episode(db, "user", "コーヒーは深煎りが好き", "s1")
    db.commit()

    client = FakeRecallClient(
        keywords=["コーヒー"],
        search_history=False,
        summary="(該当なし)",
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    out = recall(db, "コーヒー何が好き?", client)
    assert out == ""
