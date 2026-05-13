"""topic_tag 近傍検索 + format のテスト."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from scripts.db.connection import EMBEDDING_DIM, connect
from scripts.db.migrate import init_db
from scripts.shared.embedding import pack
from scripts.topic.search import (
    TopicCandidate,
    format_topic_block,
    search_topic_candidates,
)


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / "p.db"
    init_db(p)
    conn = connect(p)
    yield conn
    conn.close()


def _vec(seed: int) -> list[float]:
    """768 次元の単位ベクトル (1 軸だけ立てる)."""
    v = [0.0] * EMBEDDING_DIM
    v[seed % EMBEDDING_DIM] = 1.0
    return v


def _seed_topic(db, topic_id: str, title: str, summary: str, tags: list[tuple[str, int]]):
    db.execute(
        "INSERT INTO topics(id, title, summary) VALUES (?,?,?)",
        (topic_id, title, summary),
    )
    for tag, vec_seed in tags:
        cur = db.execute(
            "INSERT INTO topic_tags(topic_id, tag) VALUES (?,?)",
            (topic_id, tag),
        )
        db.execute(
            "INSERT INTO topic_tag_embeddings(topic_tag_id, embedding) VALUES (?,?)",
            (cur.lastrowid, pack(_vec(vec_seed))),
        )
    db.commit()


def test_search_returns_empty_when_no_topic_tags(db):
    out = search_topic_candidates(db, _vec(0))
    assert out == []


def test_search_finds_matching_topic(db):
    _seed_topic(db, "t1", "Renju 設計", "メンション+UI", tags=[("メンション", 5)])
    out = search_topic_candidates(db, _vec(5))
    assert len(out) == 1
    assert out[0].topic_id == "t1"
    assert out[0].matched_tags == ["メンション"]


def test_search_aggregates_hits_per_topic(db):
    _seed_topic(db, "t1", "T1", "...", tags=[
        ("a", 1), ("b", 2), ("c", 3),
    ])
    _seed_topic(db, "t2", "T2", "...", tags=[("d", 1)])
    # query が seed=1 にぴったり (d も a も hit するが distance 同じ)
    out = search_topic_candidates(db, _vec(1))
    # t1 は 2 hit (a, ?) → 強い signal. ただし d=1 と a=1 が同じ vector なので両方 0 距離.
    # 順序確認だけ: hit_count DESC
    assert out[0].hit_count >= out[-1].hit_count


def test_search_filters_by_distance(db):
    _seed_topic(db, "t-far", "Far", "x", tags=[("远い", 100)])
    out = search_topic_candidates(db, _vec(0), distance_max=0.1)
    assert out == []  # 直交 → distance 1.0 → filter out


def test_format_block_single_candidate():
    c = TopicCandidate(
        topic_id="t1", title="Renju 設計",
        summary="UI 議論", hit_count=2, best_distance=0.1,
        matched_tags=["サイドバー", "メンション"],
    )
    block = format_topic_block([c])
    assert "## 関連する議論" in block
    assert "Renju 設計" in block
    assert "topic_id=t1" in block
    assert "UI 議論" in block


def test_format_block_multi_candidates_includes_confirmation():
    cs = [
        TopicCandidate("a", "A", "summary A", 2, 0.1, ["x"]),
        TopicCandidate("b", "B", "summary B", 2, 0.2, ["y"]),
    ]
    block = format_topic_block(cs)
    assert "候補複数" in block
    assert "## 候補確認" in block
    assert "continue_topic" in block


def test_format_block_empty_returns_empty():
    assert format_topic_block([]) == ""


def test_format_block_single_no_summary_shows_placeholder():
    c = TopicCandidate("t", "Title", None, 1, 0.1, ["tag"])
    block = format_topic_block([c])
    assert "未生成" in block
