"""scripts.topic.summarize のテスト."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode
from scripts.topic.summarize import (
    build_prompt,
    fetch_topic_episodes,
    find_topic_for_session,
    find_topic_needing_summary,
    generate_topic_summary,
    parse_summary,
    summarize_topic,
)


@dataclass
class FakeSummaryClient:
    response: str = ""
    last_prompt: str = ""

    def generate(self, model, prompt, num_ctx=None):
        self.last_prompt = prompt
        return self.response

    def embed(self, model, text):
        return []


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / "p.db"
    init_db(p)
    conn = connect(p)
    yield conn
    conn.close()


def test_parse_summary_clean_json():
    t, s = parse_summary('{"title": "Renju 設計", "summary": "UI 議論"}')
    assert t == "Renju 設計"
    assert s == "UI 議論"


def test_parse_summary_with_code_fence():
    t, s = parse_summary('```json\n{"title": "X", "summary": "Y"}\n```')
    assert t == "X"
    assert s == "Y"


def test_parse_summary_invalid_returns_none():
    assert parse_summary("not json") == (None, None)
    assert parse_summary("") == (None, None)


def test_fetch_topic_episodes_orders_oldest_first(db):
    db.execute("INSERT INTO topics(id) VALUES ('t1')")
    db.commit()
    save_episode(db, role="user", content="第 1 発話", session_id="s1", topic_id="t1")
    save_episode(db, role="assistant", content="返答", session_id="s1", topic_id="t1")
    save_episode(db, role="user", content="第 2 発話", session_id="s1", topic_id="t1")
    lines = fetch_topic_episodes(db, "t1")
    assert lines[0].endswith("第 1 発話")
    assert lines[-1].endswith("第 2 発話")


def test_generate_topic_summary_returns_parsed(db):
    db.execute("INSERT INTO topics(id) VALUES ('t1')")
    db.commit()
    save_episode(db, role="user", content="UI 議論", session_id="s1", topic_id="t1")
    client = FakeSummaryClient(response='{"title": "UI", "summary": "議論進行中"}')
    title, summary = generate_topic_summary(db, "t1", client)
    assert title == "UI"
    assert summary == "議論進行中"


def test_generate_topic_summary_returns_none_when_no_episodes(db):
    db.execute("INSERT INTO topics(id) VALUES ('empty')")
    db.commit()
    client = FakeSummaryClient(response='{"title": "X", "summary": "Y"}')
    title, summary = generate_topic_summary(db, "empty", client)
    assert (title, summary) == (None, None)


def test_summarize_topic_writes_to_db(db):
    db.execute("INSERT INTO topics(id) VALUES ('t1')")
    db.commit()
    save_episode(db, role="user", content="X", session_id="s1", topic_id="t1")
    client = FakeSummaryClient(response='{"title": "Title", "summary": "Sum"}')
    ok = summarize_topic(db, "t1", client)
    assert ok is True
    row = db.execute("SELECT title, summary FROM topics WHERE id='t1'").fetchone()
    assert row[0] == "Title"
    assert row[1] == "Sum"


def test_summarize_topic_skips_when_llm_fails(db):
    db.execute("INSERT INTO topics(id) VALUES ('t1')")
    db.commit()
    save_episode(db, role="user", content="X", session_id="s1", topic_id="t1")
    @dataclass
    class CrashClient:
        def generate(self, m, p, num_ctx=None):
            raise RuntimeError("oops")
        def embed(self, m, t):
            return []
    ok = summarize_topic(db, "t1", CrashClient())
    assert ok is False
    row = db.execute("SELECT title, summary FROM topics WHERE id='t1'").fetchone()
    assert row[0] is None and row[1] is None


def test_find_topic_needing_summary_returns_oldest_unfilled(db):
    db.execute("INSERT INTO topics(id, title, summary) VALUES ('done', 'a', 'b')")
    db.execute("INSERT INTO topics(id, summary) VALUES ('pending', '')")
    db.commit()
    assert find_topic_needing_summary(db) == "pending"


def test_find_topic_needing_summary_returns_none_when_all_filled(db):
    db.execute("INSERT INTO topics(id, title, summary) VALUES ('a', 'A', 'sa')")
    db.commit()
    assert find_topic_needing_summary(db) is None


def test_find_topic_for_session_uses_meta_override(db):
    db.execute("INSERT INTO topics(id) VALUES ('past')")
    db.execute("INSERT INTO meta(key, value) VALUES ('topic_for_session_s1', 'past')")
    db.commit()
    assert find_topic_for_session(db, "s1") == "past"


def test_find_topic_for_session_falls_back_to_episode(db):
    db.execute("INSERT INTO topics(id) VALUES ('t-from-ep')")
    db.execute(
        "INSERT INTO episodes(role, content, session_id, topic_id) VALUES (?,?,?,?)",
        ("user", "x", "s1", "t-from-ep"),
    )
    db.commit()
    assert find_topic_for_session(db, "s1") == "t-from-ep"


def test_find_topic_for_session_none_when_no_data(db):
    assert find_topic_for_session(db, "missing") is None
