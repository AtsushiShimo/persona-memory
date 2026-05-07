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
from scripts.recall.run import _wants_episode_search, recall
from scripts.recall.search import (
    RecalledEpisode,
    search_episodes_by_keywords,
)


@dataclass
class FakeRecallClient:
    keywords: list[str]
    embedding_map: dict[str, list[float]] = field(default_factory=dict)
    default_embedding: list[float] = field(default_factory=lambda: [0.0] * 768)

    def generate(self, model, prompt):
        return json.dumps(self.keywords, ensure_ascii=False)

    def embed(self, model, text):
        return list(self.embedding_map.get(text, self.default_embedding))


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


# ── trigger 検出 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "過去の会話を検索して",
    "前回話した内容覚えてる?",
    "履歴を見せて",
    "やり取りを思い出して",
    "あの時何話したっけ",
    "先週の会話を辿りたい",
    "previous conversation",
    "conversation history",
])
def test_wants_episode_search_positive(text):
    assert _wants_episode_search(text) is True


@pytest.mark.parametrize("text", [
    "コーヒーは深煎りが好き",
    "今日の天気は?",
    "Python のリスト内包表記教えて",
    "",
])
def test_wants_episode_search_negative(text):
    assert _wants_episode_search(text) is False


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

def test_recall_searches_episodes_when_triggered(db):
    save_episode(db, "user", "コーヒーは深煎りが好き", "s1")
    save_episode(db, "user", "履歴を見せて", "s1")  # 現発話相当も episodes に入れておく
    db.commit()

    client = FakeRecallClient(
        keywords=["コーヒー"],
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    out = recall(db, "履歴の中でコーヒーの話あった?", client)
    assert "## 関連する過去の会話" in out
    assert "深煎り" in out


def test_recall_skips_episodes_without_trigger(db):
    save_episode(db, "user", "コーヒーは深煎りが好き", "s1")
    db.commit()

    client = FakeRecallClient(
        keywords=["コーヒー"],
        embedding_map={"コーヒー": [1.0] + [0.0] * 767},
    )
    # トリガー語なし → episodes は検索されない
    out = recall(db, "コーヒー何が好き?", client)
    assert "## 関連する過去の会話" not in out
