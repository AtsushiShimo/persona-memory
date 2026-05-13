"""scripts.db_cozo.recall — vec hit + 流れ再構築 のテスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db_cozo.connection import EMBEDDING_DIM, init_db, next_id
from scripts.db_cozo.discussion import add_edge, add_node
from scripts.db_cozo.recall import (
    TopicCandidate,
    aggregate_topic_candidates,
    format_flow_block,
    format_multi_candidates_block,
    recall_topic_flow,
)
from scripts.db_cozo.repo import (
    ensure_topic, save_episode, set_active_topic,
)
from scripts.shared.embedding import pack


@pytest.fixture
def client(tmp_path: Path):
    return init_db(tmp_path / "p.cozo.db")


def _vec(seed: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[seed % EMBEDDING_DIM] = 1.0
    return v


def _seed_topic_with_tags(client, topic_id: str, title: str, summary: str,
                          tags: list[tuple[str, int]]):
    ensure_topic(client, topic_id)
    client.run(
        "?[id, title, summary, created_at, last_active_at] <- "
        "[[$id, $t, $s, '2026-05-13', '2026-05-13']] "
        ":put topic {id => title, summary, created_at, last_active_at}",
        {"id": topic_id, "t": title, "s": summary},
    )
    for tag, vec_seed in tags:
        tid = next_id(client, "topic_tag")
        client.run(
            "?[id, topic_id, tag, ts, embedding] <- "
            "[[$id, $tp, $tag, '2026-05-13', vec($e)]] "
            ":put topic_tag {id => topic_id, tag, ts, embedding}",
            {"id": tid, "tp": topic_id, "tag": tag, "e": _vec(vec_seed)},
        )


# ── aggregate_topic_candidates ──

def test_aggregate_returns_empty_for_no_hits():
    assert aggregate_topic_candidates([], {}) == []


def test_aggregate_groups_by_topic_id_and_sorts_by_hit_count():
    hits = [
        {"topic_id": "a", "tag": "x", "distance": 0.1},
        {"topic_id": "a", "tag": "y", "distance": 0.2},
        {"topic_id": "b", "tag": "z", "distance": 0.05},
    ]
    info = {"a": {"title": "A", "summary": "sa"},
            "b": {"title": "B", "summary": "sb"}}
    out = aggregate_topic_candidates(hits, info)
    assert out[0].topic_id == "a"  # hit_count=2 が最初
    assert out[0].matched_tags == ["x", "y"]
    assert out[1].topic_id == "b"


def test_aggregate_caps_at_max_topics():
    hits = [{"topic_id": str(i), "tag": "x", "distance": 0.1} for i in range(10)]
    info = {str(i): {"title": None, "summary": None} for i in range(10)}
    out = aggregate_topic_candidates(hits, info, max_topics=3)
    assert len(out) == 3


# ── format ──

def test_format_flow_block_includes_chain_and_episodes():
    c = TopicCandidate("t1", "Renju 設計", "UI 議論", ["メンション"], 1, 0.1)
    chain = [
        {"depth": 0, "id": 1, "kind": "topic", "title": "プロダクト名", "state": "proposed"},
        {"depth": 1, "id": 2, "kind": "decision", "title": "Renju 採用", "state": "accepted"},
    ]
    last = [{"id": 5, "role": "assistant", "content": "次論点 4 つ"}]
    out = format_flow_block(c, chain, last)
    assert "Renju 設計" in out
    assert "Renju 採用" in out
    assert "次論点 4 つ" in out


def test_format_flow_block_handles_empty_chain():
    c = TopicCandidate("t1", "T", "S", ["x"], 1, 0.1)
    out = format_flow_block(c, [], [])
    assert "## 関連する議論" in out


def test_format_multi_candidates_includes_confirmation():
    cs = [
        TopicCandidate("a", "A", "sa", ["x"], 2, 0.1),
        TopicCandidate("b", "B", "sb", ["y"], 2, 0.1),
    ]
    out = format_multi_candidates_block(cs)
    assert "候補複数" in out and "## 候補確認" in out and "continue_topic" in out


# ── recall_topic_flow end-to-end ──

def test_recall_topic_flow_returns_empty_when_no_tags(client):
    out = recall_topic_flow(client, _vec(0))
    assert out == ""


def test_recall_topic_flow_returns_flow_when_single_topic_hit(client):
    _seed_topic_with_tags(client, "t-renju", "Renju 設計", "UI 議論進行中",
                          tags=[("メンション", 30)])
    ensure_topic(client, "t-renju")
    set_active_topic(client, "s1", "t-renju")
    save_episode(client, role="user", content="プロダクト名出し", session_id="s1")
    save_episode(client, role="assistant", content="Renju 採用", session_id="s1")
    save_episode(client, role="assistant", content="次論点 4 つ", session_id="s1")
    n1 = add_node(client, kind="topic", title="プロダクト名候補",
                  episode_id=1, embedding=_vec(28))
    n2 = add_node(client, kind="decision", title="Renju 採用", state="accepted",
                  episode_id=2, embedding=_vec(29))
    n3 = add_node(client, kind="observation", title="次論点 4 つ提示",
                  episode_id=3, embedding=_vec(30))
    add_edge(client, n1, n2, "決定")
    add_edge(client, n2, n3, "次バトン")

    out = recall_topic_flow(client, _vec(30))
    assert "Renju 設計" in out
    assert "次論点 4 つ提示" in out
    assert "## 関連する議論" in out
