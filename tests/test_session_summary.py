"""session summary 生成 + 永続化のテスト."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode
from scripts.escalate.claude_p import EscalationResult
from scripts.summary.session import (
    MIN_TURNS_FOR_SUMMARY,
    build_summary_prompt,
    fetch_session_turns,
    generate_session_summary,
    upsert_session_summary_fact,
)


@dataclass
class FakeEmbedClient:
    vec: list[float] = None

    def __post_init__(self):
        if self.vec is None:
            self.vec = [0.1] * 768

    def embed(self, model, text):
        return list(self.vec)


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


def _add_turns(db, session_id, n):
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        save_episode(db, role, f"発話 {i}", session_id)


# ── fetch_session_turns ─────────────────────────────────────────────────────

def test_fetch_session_turns_returns_all_in_order(db):
    _add_turns(db, "s1", 4)
    _add_turns(db, "s2", 3)  # 別 session は除外
    turns = fetch_session_turns(db, "s1")
    assert len(turns) == 4
    assert all(t["session_id"] if "session_id" in t else True for t in turns)
    assert turns[0]["content"] == "発話 0"
    assert turns[3]["content"] == "発話 3"


# ── build_summary_prompt ────────────────────────────────────────────────────

def test_build_summary_prompt_contains_transcript_and_instructions():
    turns = [
        {"id": 1, "role": "user", "content": "コーヒーは深煎り",
         "timestamp": "2026-05-08 10:00:00"},
        {"id": 2, "role": "assistant", "content": "了解",
         "timestamp": "2026-05-08 10:00:30"},
    ]
    prompt = build_summary_prompt(turns)
    assert "深煎り" in prompt
    assert "[user]" in prompt
    assert "[assistant]" in prompt
    assert "要約" in prompt
    assert "禁止" in prompt or "不要" in prompt or "含めない" in prompt


# ── upsert_session_summary_fact ─────────────────────────────────────────────

def test_upsert_session_summary_creates_new_fact(db):
    fid = upsert_session_summary_fact(db, "abc123def456", "今日の議論要約",
                                       FakeEmbedClient())
    assert fid is not None

    row = db.execute(
        "SELECT category, key, value, importance, status FROM facts WHERE id=?",
        (fid,),
    ).fetchone()
    assert row[0] == "context"
    assert row[1] == "session_summary_abc123def456"  # short_id 12 文字
    assert row[2] == "今日の議論要約"
    assert row[3] == 7
    assert row[4] == "active"


def test_upsert_session_summary_supersedes_existing(db):
    fid1 = upsert_session_summary_fact(db, "abc123def456", "古い要約",
                                        FakeEmbedClient())
    fid2 = upsert_session_summary_fact(db, "abc123def456", "新しい要約",
                                        FakeEmbedClient())
    assert fid1 != fid2

    rows = db.execute(
        "SELECT id, value, status, supersedes, superseded_by "
        "FROM facts WHERE key='session_summary_abc123def456' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    old, new = rows
    assert old[2] == "superseded"
    assert old[4] == new[0]
    assert new[2] == "active"
    assert new[3] == old[0]


def test_upsert_session_summary_skips_empty(db):
    fid = upsert_session_summary_fact(db, "s1", "", FakeEmbedClient())
    assert fid is None
    n = db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    assert n == 0


def test_upsert_session_summary_writes_embedding(db):
    fid = upsert_session_summary_fact(db, "s12", "要約本文",
                                       FakeEmbedClient())
    row = db.execute(
        "SELECT fact_id FROM fact_embeddings WHERE fact_id=?", (fid,)
    ).fetchone()
    assert row is not None


# ── generate_session_summary (full path) ────────────────────────────────────

def test_generate_skips_below_threshold(db):
    """ターン数が閾値未満なら summary skip."""
    _add_turns(db, "s1", MIN_TURNS_FOR_SUMMARY - 1)
    fid = generate_session_summary(db, "s1", FakeEmbedClient())
    assert fid is None
    n = db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    assert n == 0


def test_generate_calls_claude_and_persists(db):
    _add_turns(db, "s1abc123def", MIN_TURNS_FOR_SUMMARY + 2)
    fake_result = EscalationResult(text="議論の要約", success=True)
    with patch("scripts.summary.session.invoke_claude", return_value=fake_result):
        fid = generate_session_summary(db, "s1abc123def", FakeEmbedClient())
    assert fid is not None
    row = db.execute(
        "SELECT value FROM facts WHERE id=?", (fid,)
    ).fetchone()
    assert row[0] == "議論の要約"


def test_generate_skips_on_claude_failure(db):
    _add_turns(db, "s1", MIN_TURNS_FOR_SUMMARY + 2)
    fake_result = EscalationResult(text="", success=False, error="timeout")
    with patch("scripts.summary.session.invoke_claude", return_value=fake_result):
        fid = generate_session_summary(db, "s1", FakeEmbedClient())
    assert fid is None
