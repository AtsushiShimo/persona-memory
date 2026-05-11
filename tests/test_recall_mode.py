"""PERSONA_RECALL_MODE 切替のテスト (0.6.4).

ローカル LLM (gemma3:12b) の弁別力不足を回避するため、 recall 出力を
3 mode で切り替え可能にした:
- summarize    (default): ローカル LLM curate
- index_titled         : fact_id + title (本文ゼロ寄り)
- index_only           : fact_id のみ (本文ゼロ)
"""
from __future__ import annotations

from dataclasses import dataclass

from scripts.recall.run import _format_index


@dataclass
class _Fact:
    fact_id: int
    category: str
    key: str
    value: str
    importance: int = 5
    access_count: int = 0
    distance: float = 0.3
    score: float = 0.5
    retracted_value: str | None = None


@dataclass
class _Episode:
    episode_id: int
    role: str
    content: str
    timestamp: str


def test_index_titled_includes_fact_id_and_title():
    hits = [_Fact(101, "context", "renju_default_category_name",
                  "Renju の UI: デフォルトカテゴリ名は「全体」 で確定")]
    eps = [_Episode(501, "user", "renju の議論どこまで覚えてる？",
                    "2026-05-11 17:20:00")]
    out = _format_index(hits, eps, mode="index_titled")
    assert "#101" in out
    assert "context/renju_default_category_name" in out
    assert "Renju の UI: デフォルトカテゴリ名は「全体" in out  # 30 字以内のタイトル
    assert "ep#501" in out
    assert "2026-05-11 17:20:00" in out
    assert "search_memory" in out  # 深掘り注記


def test_index_only_excludes_titles():
    hits = [_Fact(101, "context", "x", "secret detail body")]
    eps = [_Episode(501, "user", "secret content body", "2026-05-11 17:20")]
    out = _format_index(hits, eps, mode="index_only")
    assert "#101" in out
    assert "ep#501" in out
    # 本文 / タイトルは含まれない
    assert "secret detail body" not in out
    assert "secret content body" not in out
    assert "search_memory" in out  # main agent への明示誘導


def test_index_titled_empty_returns_empty_string():
    assert _format_index([], [], mode="index_titled") == ""


def test_index_only_empty_returns_empty_string():
    assert _format_index([], [], mode="index_only") == ""


def test_index_titled_truncates_long_values():
    """value が長くても 30 字 + ellipsis でカット."""
    long_value = "あ" * 200
    hits = [_Fact(1, "context", "long", long_value)]
    out = _format_index(hits, [], mode="index_titled")
    # 30 字に切られている (ellipsis 含めて 35 字以内程度)
    line = next(l for l in out.split("\n") if l.startswith("- #1"))
    assert long_value not in line
    assert "あ" * 30 in line


def test_index_titled_only_facts_no_episodes():
    hits = [_Fact(1, "context", "k", "v")]
    out = _format_index(hits, [], mode="index_titled")
    assert "記憶" in out
    assert "会話履歴" not in out  # episode セクションは出ない


def test_index_titled_only_episodes_no_facts():
    eps = [_Episode(1, "user", "content", "2026-05-11")]
    out = _format_index([], eps, mode="index_titled")
    assert "会話履歴" in out
    assert "(記憶)" not in out  # fact セクションは出ない
