"""Phase 5: boot 層注入 + dirty 再注入 + condense 警告のテスト。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.boot.inject import (
    BOOT_CATEGORIES,
    clear_dirty,
    condense_warning_if_needed,
    estimate_tokens,
    fetch_boot_facts,
    format_boot_facts,
    is_dirty,
    mark_dirty,
)
from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.write.extract import FactCandidate
from scripts.write.persist import apply_candidate, insert_new
from scripts.write.similarity import find_by_key

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


def test_fetch_boot_facts_only_persona_and_rule(db):
    insert_new(db, FactCandidate("persona", "style", "率直に", 9), [0.0] * 768)
    insert_new(db, FactCandidate("rule", "no_force_push", "main に force push 禁止", 9), [0.0] * 768)
    insert_new(db, FactCandidate("preference", "coffee", "深煎り", 6), [0.0] * 768)
    db.commit()

    facts = fetch_boot_facts(db)
    cats = {f["category"] for f in facts}
    assert cats == {"persona", "rule"}
    assert len(facts) == 2


def test_fetch_boot_facts_excludes_superseded(db):
    insert_new(db, FactCandidate("persona", "style", "old", 5), [0.0] * 768)
    db.execute("UPDATE facts SET status='superseded'")
    db.commit()
    assert fetch_boot_facts(db) == []


def test_format_boot_facts_includes_marker():
    facts = [
        {"category": "persona", "key": "style", "value": "率直に", "importance": 9},
    ]
    text = format_boot_facts(facts)
    assert "boot 層" in text
    assert "[persona/style]" in text
    assert "率直に" in text


def test_format_boot_facts_empty():
    assert format_boot_facts([]) == ""


# ── dirty フラグ ─────────────────────────────────────────────────────────────

def test_dirty_flag_lifecycle(db):
    assert is_dirty(db) is False
    mark_dirty(db)
    assert is_dirty(db) is True
    clear_dirty(db)
    assert is_dirty(db) is False


def test_apply_candidate_marks_dirty_for_boot_layer(db):
    cand = FactCandidate("persona", "style", "率直に指摘", 9)
    apply_candidate(db, cand, match=None, embedding=[0.0] * 768)
    assert is_dirty(db) is True


def test_apply_candidate_does_not_mark_dirty_for_dynamic_layer(db):
    cand = FactCandidate("preference", "coffee", "深煎り", 6)
    apply_candidate(db, cand, match=None, embedding=[0.0] * 768)
    assert is_dirty(db) is False


def test_apply_candidate_marks_dirty_on_supersede(db):
    insert_new(db, FactCandidate("rule", "branch_protection", "main 直 push 不可", 9), [0.0] * 768)
    db.commit()
    match = find_by_key(db, "rule", "branch_protection")
    cand = FactCandidate("rule", "branch_protection", "完全に違う規則", 9)
    apply_candidate(db, cand, match, embedding=[0.5] * 768)
    assert is_dirty(db) is True


# ── condense 警告 ───────────────────────────────────────────────────────────

def test_condense_warning_below_threshold(db):
    insert_new(db, FactCandidate("persona", "style", "短く", 9), [0.0] * 768)
    db.commit()
    assert condense_warning_if_needed(db) == ""


def test_condense_warning_above_facts_threshold(db):
    for i in range(60):  # > 50
        insert_new(db, FactCandidate("persona", f"key_{i}", "短い指示", 5), [0.0] * 768)
    db.commit()
    warn = condense_warning_if_needed(db)
    assert "肥大化" in warn
    assert "/persona-memory:condense" in warn


def test_condense_warning_above_tokens_threshold(db):
    # 5 個 × 各 2200 tokens 想定の長文 → > 5000
    long_value = "あ" * 5000
    for i in range(5):
        insert_new(db, FactCandidate("persona", f"big_{i}", long_value, 5), [0.0] * 768)
    db.commit()
    warn = condense_warning_if_needed(db)
    assert "肥大化" in warn


def test_estimate_tokens_returns_positive():
    facts = [{"category": "persona", "key": "k", "value": "x" * 100, "importance": 5}]
    assert estimate_tokens(facts) > 0


# ── SessionStart hook 統合テスト ─────────────────────────────────────────────

def _run_session_start(db_path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PERSONA_MEMORY_DB"] = str(db_path)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "scripts.hooks.on_session_start"],
        input=b"{}",  # SessionStart hook は payload あまり使わない
        env=env,
        capture_output=True,
    )


def test_session_start_emits_boot_layer(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        insert_new(conn, FactCandidate("persona", "style", "率直に指摘", 9), [0.0] * 768)
        insert_new(conn, FactCandidate("rule", "no_force_push", "main 禁止", 9), [0.0] * 768)
        conn.commit()
    finally:
        conn.close()

    r = _run_session_start(db_path)
    assert r.returncode == 0
    out = r.stdout.decode()
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "[persona/style]" in ctx
    assert "[rule/no_force_push]" in ctx
    assert "率直に指摘" in ctx


def test_session_start_no_output_when_empty(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    r = _run_session_start(db_path)
    assert r.returncode == 0
    # boot 層なし → stdout 空 (additionalContext 出力なし)
    assert r.stdout.strip() == b""


def test_session_start_clears_dirty_flag(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        insert_new(conn, FactCandidate("persona", "style", "rude", 9), [0.0] * 768)
        mark_dirty(conn)
        conn.commit()
        assert is_dirty(conn) is True
    finally:
        conn.close()

    _run_session_start(db_path)

    conn = connect(db_path)
    try:
        assert is_dirty(conn) is False
    finally:
        conn.close()
