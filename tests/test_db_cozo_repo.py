"""scripts/db_cozo/repo.py のテスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db_cozo.connection import EMBEDDING_DIM, init_db, next_id
from scripts.db_cozo.repo import (
    ensure_topic,
    fetch_buffer,
    fetch_episode,
    fetch_recent_episodes_for_topic,
    fetch_topic_episodes,
    fetch_topics,
    get_active_topic,
    get_meta,
    save_episode,
    search_episodes_vec,
    search_facts_vec,
    search_topic_tags_vec,
    set_active_topic,
    set_meta,
)


@pytest.fixture
def client(tmp_path: Path):
    db = tmp_path / "p.cozo.db"
    return init_db(db)


def _vec(seed: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[seed % EMBEDDING_DIM] = 1.0
    return v


def test_save_episode_returns_int_id_starting_at_1(client):
    e1 = save_episode(client, role="user", content="hi", session_id="s1")
    e2 = save_episode(client, role="assistant", content="yo", session_id="s1")
    assert e1 == 1
    assert e2 == 2


def test_save_episode_default_topic_id_is_session_id(client):
    eid = save_episode(client, role="user", content="x", session_id="sX")
    ep = fetch_episode(client, eid)
    assert ep["topic_id"] == "sX"


def test_save_episode_respects_continue_topic_override(client):
    ensure_topic(client, "past")
    set_active_topic(client, "sNew", "past")
    eid = save_episode(client, role="user", content="続き", session_id="sNew")
    ep = fetch_episode(client, eid)
    assert ep["topic_id"] == "past"


def test_save_episode_topic_disable_keeps_topic_null(client, monkeypatch):
    monkeypatch.setenv("PERSONA_TOPIC_DISABLE", "1")
    eid = save_episode(client, role="user", content="x", session_id="s1")
    ep = fetch_episode(client, eid)
    assert ep["topic_id"] is None


def test_meta_get_set(client):
    assert get_meta(client, "missing") is None
    set_meta(client, "k", "v1")
    assert get_meta(client, "k") == "v1"
    set_meta(client, "k", "v2")  # upsert
    assert get_meta(client, "k") == "v2"


def test_fetch_buffer_returns_oldest_first_within_session(client):
    save_episode(client, role="user", content="a", session_id="s1")
    save_episode(client, role="assistant", content="b", session_id="s1")
    save_episode(client, role="user", content="other", session_id="s2")
    save_episode(client, role="user", content="c", session_id="s1")
    buf = fetch_buffer(client, 99, 5, "s1")
    contents = [b["content"] for b in buf]
    assert contents == ["a", "b", "c"]


def test_search_facts_vec_returns_nearest_active(client):
    e_match = _vec(7)
    e_other = _vec(500)
    fid1 = next_id(client, "fact")
    fid2 = next_id(client, "fact")
    client.run(
        "?[id, category, key, value, importance, created_at, updated_at, embedding] <- "
        "[[$id, 'preference', 'a', 'matched', 5, 't', 't', vec($e)]] "
        ":put fact {id => category, key, value, importance, created_at, updated_at, embedding}",
        {"id": fid1, "e": e_match},
    )
    client.run(
        "?[id, category, key, value, importance, created_at, updated_at, embedding] <- "
        "[[$id, 'preference', 'b', 'far', 5, 't', 't', vec($e)]] "
        ":put fact {id => category, key, value, importance, created_at, updated_at, embedding}",
        {"id": fid2, "e": e_other},
    )
    hits = search_facts_vec(client, e_match, top_k=3, distance_max=0.6)
    assert hits[0]["value"] == "matched"
    assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-4)


def test_search_topic_tags_vec_filters_by_distance(client):
    ensure_topic(client, "t1")
    e = _vec(10)
    tid = next_id(client, "topic_tag")
    client.run(
        "?[id, topic_id, tag, ts, embedding] <- "
        "[[$id, 't1', 'メンション設計', '2026-05-13', vec($e)]] "
        ":put topic_tag {id => topic_id, tag, ts, embedding}",
        {"id": tid, "e": e},
    )
    hits = search_topic_tags_vec(client, e, top_k=5, distance_max=0.1)
    assert any(h["topic_id"] == "t1" for h in hits)


def test_fetch_topics_returns_dict_keyed_by_id(client):
    client.run(
        "?[id, title, summary, created_at, last_active_at] <- "
        "[['t1', 'A', 'sa', 't', 't'], ['t2', 'B', 'sb', 't', 't']] "
        ":put topic {id => title, summary, created_at, last_active_at}",
    )
    out = fetch_topics(client, ["t1", "t2", "missing"])
    assert out["t1"]["title"] == "A"
    assert out["t2"]["summary"] == "sb"
    assert "missing" not in out


def test_fetch_topic_episodes_returns_oldest_first(client):
    ensure_topic(client, "t1")
    set_active_topic(client, "s1", "t1")
    save_episode(client, role="user", content="e1", session_id="s1")
    save_episode(client, role="assistant", content="e2", session_id="s1")
    save_episode(client, role="user", content="e3", session_id="s1")
    out = fetch_topic_episodes(client, "t1", limit=10)
    assert [r["content"] for r in out] == ["e1", "e2", "e3"]


def test_fetch_recent_episodes_for_topic_returns_newest_first(client):
    ensure_topic(client, "t1")
    set_active_topic(client, "s1", "t1")
    for c in "abcde":
        save_episode(client, role="user", content=c, session_id="s1")
    out = fetch_recent_episodes_for_topic(client, "t1", last_n=3)
    assert [r["content"] for r in out] == ["e", "d", "c"]
