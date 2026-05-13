"""scripts.db_cozo.recall_full のテスト."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db_cozo.connection import EMBEDDING_DIM, init_db, next_id
from scripts.db_cozo.discussion import add_edge, add_node
from scripts.db_cozo.recall_full import recall_full
from scripts.db_cozo.repo import (
    ensure_topic, save_episode, set_active_topic,
)


@pytest.fixture
def client(tmp_path: Path):
    return init_db(tmp_path / "p.cozo.db")


def _vec(seed: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[seed % EMBEDDING_DIM] = 1.0
    return v


@dataclass
class FakeRecallClient:
    """analyze + summarize 両方に対応する FakeClient."""
    analyze_response: str = '{"keywords": ["x"], "search_history": false, "trigger_phrase": null}'
    summary_response: str = "深煎り好き"
    embed_seed: int = 7

    def generate(self, model, prompt, num_ctx=None):
        if "search_history" in prompt:
            return self.analyze_response
        return self.summary_response

    def embed(self, model, text):
        return _vec(self.embed_seed)


def _seed_topic_with_tags(client, topic_id, title, summary, tags):
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


def _seed_fact(client, category, key, value, importance, embedding):
    fid = next_id(client, "fact")
    client.run(
        "?[id, category, key, value, importance, created_at, updated_at, embedding] "
        "<- [[$id, $c, $k, $v, $i, '2026-05-13', '2026-05-13', vec($e)]] "
        ":put fact {id => category, key, value, importance, created_at, updated_at, embedding}",
        {"id": fid, "c": category, "k": key, "v": value, "i": importance, "e": embedding},
    )
    return fid


def test_recall_full_returns_empty_when_no_data(client):
    fake = FakeRecallClient(embed_seed=999)
    out = recall_full(client, "hello", fake)
    assert out == ""


def test_recall_full_returns_topic_block_when_topic_hits(client):
    _seed_topic_with_tags(client, "t-renju", "Renju", "UI 議論",
                          tags=[("メンション", 7)])
    fake = FakeRecallClient(embed_seed=7)
    out = recall_full(client, "メンション", fake)
    assert "Renju" in out


def test_recall_full_returns_fact_summary_when_fact_hits(client):
    _seed_fact(client, "preference", "coffee", "深煎り", 6, _vec(7))
    fake = FakeRecallClient(
        analyze_response='{"keywords": ["coffee"], "search_history": false, "trigger_phrase": null}',
        summary_response="マスターは深煎り好き",
        embed_seed=7,
    )
    out = recall_full(client, "コーヒーは?", fake)
    assert "## 思い出した記憶" in out
    assert "深煎り" in out


def test_recall_full_combines_topic_and_fact_blocks(client):
    _seed_topic_with_tags(client, "t1", "Renju", "ui", tags=[("メンション", 7)])
    _seed_fact(client, "preference", "coffee", "深煎り", 6, _vec(7))
    fake = FakeRecallClient(
        analyze_response='{"keywords": ["x"], "search_history": false, "trigger_phrase": null}',
        summary_response="深煎り",
        embed_seed=7,
    )
    out = recall_full(client, "メンションとコーヒー", fake)
    # 両ブロックを含む
    assert "Renju" in out
    assert "## 思い出した記憶" in out


def test_recall_full_includes_episode_search_when_history_query(client):
    fake = FakeRecallClient(
        analyze_response='{"keywords": ["前"], "search_history": true, "trigger_phrase": "前の話"}',
        summary_response="前回 X と話した",
        embed_seed=7,
    )
    set_active_topic(client, "s1", "t1")
    ensure_topic(client, "t1")
    save_episode(client, role="user", content="X" * 100, session_id="s1")
    # episode embedding 後付け
    client.run(
        "?[id, role, content, session_id, topic_id, timestamp, embedding] := "
        "*episode{id, role, content, session_id, topic_id, timestamp}, "
        "id = 1, embedding = vec($e) "
        ":put episode {id => role, content, session_id, topic_id, timestamp, embedding}",
        {"e": _vec(7)},
    )
    out = recall_full(client, "前の話", fake)
    assert "前回" in out
