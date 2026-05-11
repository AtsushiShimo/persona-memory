"""episodes_fts (FTS5 trigram BM25) のテスト — 0.6.0 phase3.

vec0 cosine の弱点 (短文 / 固有名詞 / typo) を BM25 で補う補完経路を検証.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.recall.search import RecalledEpisode, search_episodes_by_fts


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "p.db"
    init_db(p)
    return p


def _seed_episodes(conn, samples: list[tuple[str, str]]) -> list[int]:
    """(role, content) のリストを INSERT して id を返す."""
    ids = []
    for role, content in samples:
        cur = conn.execute(
            "INSERT INTO episodes(role, content, session_id) VALUES (?, ?, 's1')",
            (role, content),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


# ── schema 直後の構造確認 ────────────────────────────────────────────────────

def test_episodes_fts_table_exists_in_fresh_db(db_path: Path):
    """init_db で episodes_fts 仮想テーブルが作られる."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='episodes_fts'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_trigger_syncs_on_insert(db_path: Path):
    """episodes に INSERT すると trigger で episodes_fts に同期される."""
    conn = connect(db_path)
    try:
        _seed_episodes(conn, [("user", "Renju の設計について議論しました")])
        n = conn.execute("SELECT COUNT(*) FROM episodes_fts").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_trigger_syncs_on_delete(db_path: Path):
    conn = connect(db_path)
    try:
        ids = _seed_episodes(conn, [("user", "hello world")])
        conn.execute("DELETE FROM episodes WHERE id=?", (ids[0],))
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM episodes_fts").fetchone()[0]
        assert n == 0
    finally:
        conn.close()


# ── FTS5 検索の効力 ─────────────────────────────────────────────────────────

def test_fts_hits_proper_noun_renju(db_path: Path):
    """固有名詞「Renju」 が trigram FTS5 で確実に hit する.

    vec0 cosine では nomic-embed-text の弁別力不足で漏れていた典型ケース.
    """
    conn = connect(db_path)
    try:
        _seed_episodes(conn, [
            ("user", "コーヒーは深煎り派です"),
            ("user", "Renju の設計でどこまで進んでいたか覚えていますか"),
            ("user", "明日の天気はどうかな"),
        ])
        hits = search_episodes_by_fts(conn, ["Renju"])
    finally:
        conn.close()
    assert len(hits) == 1
    assert "Renju" in hits[0].content


def test_fts_hits_japanese_partial_match(db_path: Path):
    """日本語の連続文 (単語境界なし) でも trigram でヒットする."""
    conn = connect(db_path)
    try:
        _seed_episodes(conn, [
            ("user", "デフォルトカテゴリ名は「全体」、デフォルトトピック名は「全体トピック」で確定"),
            ("user", "Stitch でレイアウトが崩れた"),
            ("user", "コーヒーの好み"),
        ])
        hits = search_episodes_by_fts(conn, ["デフォルトカテゴリ"])
    finally:
        conn.close()
    assert any("デフォルトカテゴリ" in h.content for h in hits)


def test_fts_no_match_returns_empty(db_path: Path):
    """マッチしないクエリは空リスト."""
    conn = connect(db_path)
    try:
        _seed_episodes(conn, [("user", "コーヒー")])
        hits = search_episodes_by_fts(conn, ["完全に無関係なキーワード"])
    finally:
        conn.close()
    assert hits == []


def test_fts_empty_queries_returns_empty(db_path: Path):
    conn = connect(db_path)
    try:
        assert search_episodes_by_fts(conn, []) == []
        assert search_episodes_by_fts(conn, ["", "  "]) == []
    finally:
        conn.close()


def test_fts_handles_quotes_safely(db_path: Path):
    """クエリ内のダブルクォートを escape してエラーにならない."""
    conn = connect(db_path)
    try:
        _seed_episodes(conn, [("user", '彼は "Renju" と呼んだ')])
        hits = search_episodes_by_fts(conn, ['"Renju"'])
        # クラッシュせず、 何か返るか空かのいずれか (FTS5 phrase 仕様)
        assert isinstance(hits, list)
    finally:
        conn.close()


# ── 後方互換: FTS5 が無い古い DB でも search が落ちない ─────────────────

def test_fts_returns_empty_when_fts_table_missing(tmp_path: Path):
    """episodes_fts が無い古い DB に対する FTS 検索は空リストを返す."""
    db_path = tmp_path / "old.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        # FTS5 を意図的に drop
        conn.execute("DROP TABLE IF EXISTS episodes_fts")
        conn.commit()
        hits = search_episodes_by_fts(conn, ["anything"])
    finally:
        conn.close()
    assert hits == []
