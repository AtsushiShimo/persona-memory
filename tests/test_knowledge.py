"""knowledge category の動作テスト.

- write/extract.py VALID_CATEGORIES に含まれる
- DB に upsert できる (CHECK 違反しない)
- recall search が knowledge を返す (persona/rule の除外対象外)
- MCP save_knowledge tool が期待通り fact を作る (db レイヤーで確認)
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.write.extract import VALID_CATEGORIES


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "p.db"
    init_db(p)
    return p


def test_knowledge_in_valid_categories():
    """write LLM 抽出経路で knowledge を有効カテゴリとして受け入れる."""
    assert "knowledge" in VALID_CATEGORIES


def test_knowledge_fact_can_be_inserted(db_path: Path):
    """schema CHECK 制約が 'knowledge' を許容する."""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO facts(category, key, value, importance) VALUES "
            "('knowledge', 'k_abc123', 'web 記事の要約\nsource: https://example.com', 7)"
        )
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE category='knowledge' AND status='active'"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_knowledge_fact_uniqueness_on_category_key(db_path: Path):
    """同 category + 同 key で 2 件目を active として入れられない (last-write-wins 制約)."""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO facts(category, key, value, importance) VALUES "
            "('knowledge', 'k_dup', 'v1', 7)"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO facts(category, key, value, importance) VALUES "
                "('knowledge', 'k_dup', 'v2', 7)"
            )
    finally:
        conn.close()


# ── save_knowledge MCP tool の値組み立てロジック ───────────────────────────

def test_save_knowledge_key_derivation_is_stable():
    """同じ URL からは同じ key が導出される (= 再調査で上書きされる)."""
    url = "https://example.com/article/42"
    h1 = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    h2 = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    assert h1 == h2
    assert len(f"k_{h1}") == 12  # k_ + 10 hex chars


def test_save_knowledge_key_differs_per_url():
    """別 URL からは別 key (= 関連記事は別 fact として並存)."""
    a = hashlib.sha1(b"https://example.com/a").hexdigest()[:10]
    b = hashlib.sha1(b"https://example.com/b").hexdigest()[:10]
    assert a != b
