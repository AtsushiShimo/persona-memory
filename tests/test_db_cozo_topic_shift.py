"""scripts.db_cozo.topic_shift のテスト."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.repo import (
    ensure_topic, get_active_topic, save_episode, set_active_topic,
)
from scripts.db_cozo.topic_shift import (
    ShiftJudgment, build_prompt, detect_shift, maybe_split_topic, parse_judgment,
)


@dataclass
class FakeShiftClient:
    response: str = '{"shift": false, "reason": "continuation"}'
    last_prompt: str = ""

    def generate(self, model, prompt, num_ctx=None):
        self.last_prompt = prompt
        return self.response

    def embed(self, model, text):
        return []


@pytest.fixture
def client(tmp_path: Path):
    return init_db(tmp_path / "p.cozo.db")


def test_parse_judgment_continuation():
    j = parse_judgment('{"shift": false, "reason": "same topic"}')
    assert j.shift is False


def test_parse_judgment_shift():
    j = parse_judgment('{"shift": true, "reason": "new topic"}')
    assert j.shift is True


def test_parse_judgment_invalid_json_returns_false():
    assert parse_judgment("not json").shift is False
    assert parse_judgment("").shift is False


def test_parse_judgment_with_code_fence():
    j = parse_judgment('```json\n{"shift": true, "reason": "x"}\n```')
    assert j.shift is True


def test_build_prompt_includes_recent_and_new():
    p = build_prompt(
        [{"role": "user", "content": "Renju の話"},
         {"role": "assistant", "content": "OK"}],
        "コーヒー何が好き?",
    )
    assert "Renju" in p
    assert "コーヒー" in p


def test_detect_shift_returns_false_on_empty_prompt():
    fake = FakeShiftClient()
    j = detect_shift([{"role": "user", "content": "x"}], "", fake)
    assert j.shift is False


def test_detect_shift_uses_llm():
    fake = FakeShiftClient(response='{"shift": true, "reason": "topic changed"}')
    j = detect_shift(
        [{"role": "user", "content": "Renju"}], "コーヒーは?", fake,
    )
    assert j.shift is True


# ── maybe_split_topic ──

def test_maybe_split_topic_no_split_when_few_episodes(client):
    ensure_topic(client, "t1")
    set_active_topic(client, "s1", "t1")
    save_episode(client, role="user", content="x", session_id="s1")
    fake = FakeShiftClient(response='{"shift": true, "reason": "x"}')
    out, j = maybe_split_topic(client, "s1", "別話題", fake)
    assert out == "t1"  # 蓄積不足 → split しない
    assert j is None


def test_maybe_split_topic_keeps_topic_when_continuation(client):
    ensure_topic(client, "t1")
    set_active_topic(client, "s1", "t1")
    for c in "abcd":
        save_episode(client, role="user", content=c * 100, session_id="s1")
    fake = FakeShiftClient(response='{"shift": false, "reason": "same"}')
    out, j = maybe_split_topic(client, "s1", "続き", fake)
    assert out == "t1"
    assert j is not None and j.shift is False


def test_maybe_split_topic_creates_new_topic_on_shift(client):
    ensure_topic(client, "t1")
    set_active_topic(client, "s1", "t1")
    for c in "abcd":
        save_episode(client, role="user", content=c * 100, session_id="s1")
    fake = FakeShiftClient(response='{"shift": true, "reason": "topic changed"}')
    out, j = maybe_split_topic(client, "s1", "別話題", fake)
    assert out != "t1"
    assert out.startswith("sub-")
    assert j is not None and j.shift is True
    # active topic も切替済
    assert get_active_topic(client, "s1") == out


def test_maybe_split_topic_disabled_via_env(client, monkeypatch):
    monkeypatch.setenv("PERSONA_TOPIC_SHIFT_DISABLE", "1")
    ensure_topic(client, "t1")
    set_active_topic(client, "s1", "t1")
    for c in "abcd":
        save_episode(client, role="user", content=c * 100, session_id="s1")
    fake = FakeShiftClient(response='{"shift": true, "reason": "x"}')
    out, j = maybe_split_topic(client, "s1", "別話題", fake)
    assert out == "t1"
    assert j is None
