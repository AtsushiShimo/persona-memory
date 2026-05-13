"""Phase 6: エスカレーション条件判定 + claude -p ラッパのテスト."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode
from scripts.escalate.claude_p import EscalationResult, log_escalation
from scripts.escalate.decide import (
    IMPORTANCE_THRESHOLD,
    INPUT_TOKEN_THRESHOLD,
    estimate_tokens,
    should_escalate,
)
from scripts.write.run import process_episode


# ── decide ──────────────────────────────────────────────────────────────────

def test_estimate_tokens_zero_for_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_proportional():
    assert estimate_tokens("a" * 200) == 100


def test_should_escalate_long_input():
    text = "x" * (INPUT_TOKEN_THRESHOLD * 2 + 100)
    assert should_escalate(input_text=text) == "long_input"


def test_should_escalate_high_importance():
    assert should_escalate(importance=IMPORTANCE_THRESHOLD) == "high_importance"
    assert should_escalate(importance=9) == "high_importance"


def test_should_escalate_uncertain():
    assert should_escalate(uncertain=True) == "uncertainty"


def test_should_escalate_priority_order():
    """long_input が最優先 (条件が複数満たされても reason はひとつ)."""
    text = "x" * (INPUT_TOKEN_THRESHOLD * 2 + 100)
    r = should_escalate(input_text=text, importance=9, uncertain=True)
    assert r == "long_input"


def test_should_escalate_none():
    assert should_escalate() is None
    assert should_escalate(input_text="short", importance=5) is None


# ── log_escalation ──────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


def test_log_escalation_persists(db):
    log_escalation(db, reason="long_input", caller="write", input_size=2500, outcome="ok")
    row = db.execute(
        "SELECT reason, caller, input_size, outcome FROM escalation_log"
    ).fetchone()
    assert row == ("long_input", "write", 2500, "ok")


def test_log_escalation_truncates_outcome(db):
    big = "x" * 1000
    log_escalation(db, reason="uncertainty", caller="recall", input_size=10, outcome=big)
    row = db.execute("SELECT outcome FROM escalation_log").fetchone()
    assert len(row[0]) <= 500


# ── process_episode で long_input エスカレーション ────────────────────────────

@dataclass
class FakeClient:
    facts: list

    def generate(self, model, prompt, num_ctx=None):
        return json.dumps(self.facts, ensure_ascii=False)

    def embed(self, model, text):
        return [0.0] * 768


def test_process_episode_short_input_uses_local(db):
    eid = save_episode(db, role="user", content="ok", session_id="s1")
    client = FakeClient(facts=[])
    process_episode(db, eid, buffer_n=3, client=client)
    # local LLM (FakeClient) が空を返したのでログなし
    n = db.execute("SELECT COUNT(*) FROM escalation_log").fetchone()[0]
    assert n == 0


def test_process_episode_long_input_escalates_to_claude(db):
    long_content = "あ" * (INPUT_TOKEN_THRESHOLD * 2 + 200)
    eid = save_episode(db, role="user", content=long_content, session_id="s1")
    client = FakeClient(facts=[])

    fake_result = EscalationResult(
        text=json.dumps([
            {"category": "skill", "key": "topic_x", "value": "学んだ", "importance": 6}
        ]),
        success=True,
    )
    with patch("scripts.write.run.invoke_claude", return_value=fake_result):
        results = process_episode(db, eid, buffer_n=3, client=client)

    assert len(results) == 1
    row = db.execute(
        "SELECT reason, caller FROM escalation_log ORDER BY id"
    ).fetchone()
    assert row == ("long_input", "write")


def test_process_episode_high_importance_logs_escalation(db):
    eid = save_episode(db, role="user", content="主義の話", session_id="s1")
    # local LLM が importance=9 の persona fact を返す
    client = FakeClient(facts=[
        {"category": "persona", "key": "tone", "value": "丁寧に", "importance": 9}
    ])
    # 既存の persona/tone を入れておく → match が成立 → 高 importance ログ
    from scripts.write.extract import FactCandidate
    from scripts.write.persist import insert_new
    insert_new(db, FactCandidate("persona", "tone", "敬語で", 8), [0.0] * 768)
    db.commit()

    process_episode(db, eid, buffer_n=3, client=client)

    rows = db.execute("SELECT reason FROM escalation_log").fetchall()
    reasons = [r[0] for r in rows]
    assert "high_importance" in reasons
