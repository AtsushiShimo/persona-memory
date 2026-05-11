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


# ── 0.6.5 auto mode: 会話内容で動的判定 ─────────────────────────────────────

def test_auto_mode_picks_index_titled_when_search_history_true(monkeypatch, tmp_path):
    """過去参照系発話 (search_history=True) では auto → index_titled に切替.

    full recall 呼出をモックして mode 解決のみを確認する.
    """
    from unittest.mock import patch, MagicMock
    from scripts.recall import run as run_module
    from scripts.db.connection import connect
    from scripts.db.migrate import init_db

    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        # 過去参照を判定させる
        with patch("scripts.recall.run.analyze_query") as MAQ, \
             patch("scripts.recall.run._format_index") as MFI:
            MAQ.return_value = MagicMock(search_history=True, keywords=["x"])
            MFI.return_value = "## 思い出した記憶\n- #1 fake"
            client = MagicMock()
            client.embed.return_value = [0.1] * 768
            with patch("scripts.recall.run.search", return_value=[MagicMock(fact_id=1)]):
                with patch("scripts.recall.run.search_episodes_by_embeddings", return_value=[]):
                    with patch("scripts.recall.run.search_episodes_by_fts", return_value=[]):
                        with patch("scripts.recall.run.bump_access_counts"):
                            monkeypatch.delenv("PERSONA_RECALL_MODE", raising=False)
                            run_module.recall(conn, "renju の議論どこまで覚えてる?", client)
            # _format_index が呼ばれ、 mode='index_titled' で呼ばれた
            args, kwargs = MFI.call_args
            assert kwargs.get("mode") == "index_titled"
    finally:
        conn.close()


def test_auto_mode_uses_summarize_when_search_history_false(monkeypatch, tmp_path):
    """新規話題 (search_history=False) では auto → summarize."""
    from unittest.mock import patch, MagicMock
    from scripts.recall import run as run_module
    from scripts.db.connection import connect
    from scripts.db.migrate import init_db

    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        with patch("scripts.recall.run.analyze_query") as MAQ, \
             patch("scripts.recall.run.summarize_recall") as MSR:
            MAQ.return_value = MagicMock(search_history=False, keywords=[])
            MSR.return_value = "summarized content"
            client = MagicMock()
            client.embed.return_value = [0.1] * 768
            with patch("scripts.recall.run.search", return_value=[MagicMock(fact_id=1)]):
                with patch("scripts.recall.run.search_episodes_by_embeddings", return_value=[]):
                    with patch("scripts.recall.run.search_episodes_by_fts", return_value=[]):
                        with patch("scripts.recall.run.bump_access_counts"):
                            monkeypatch.delenv("PERSONA_RECALL_MODE", raising=False)
                            out = run_module.recall(conn, "今日の天気は？", client)
            MSR.assert_called_once()
            assert "summarized content" in out
    finally:
        conn.close()


def test_explicit_env_overrides_auto(monkeypatch, tmp_path):
    """PERSONA_RECALL_MODE=summarize を明示すると、 search_history=True でも summarize."""
    from unittest.mock import patch, MagicMock
    from scripts.recall import run as run_module
    from scripts.db.connection import connect
    from scripts.db.migrate import init_db

    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        with patch("scripts.recall.run.analyze_query") as MAQ, \
             patch("scripts.recall.run.summarize_recall") as MSR:
            MAQ.return_value = MagicMock(search_history=True, keywords=["x"])
            MSR.return_value = "forced summary"
            client = MagicMock()
            client.embed.return_value = [0.1] * 768
            with patch("scripts.recall.run.search", return_value=[MagicMock(fact_id=1)]):
                with patch("scripts.recall.run.search_episodes_by_embeddings", return_value=[]):
                    with patch("scripts.recall.run.search_episodes_by_fts", return_value=[]):
                        with patch("scripts.recall.run.bump_access_counts"):
                            monkeypatch.setenv("PERSONA_RECALL_MODE", "summarize")
                            out = run_module.recall(conn, "renju 直近どこまで?", client)
            MSR.assert_called_once()
            assert "forced summary" in out
    finally:
        conn.close()
