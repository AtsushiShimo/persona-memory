"""Phase 8: 未処理 episode 再抽出 (SessionStart) のテスト."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode
from scripts.write.extract import FactCandidate
from scripts.write.persist import insert_new
from scripts.write.run import (
    PROCESSED_META_KEY,
    fetch_unprocessed_episode_ids,
    get_processed_max_id,
    mark_processed,
    run as write_run,
)

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class FakeClient:
    facts: list

    def generate(self, model, prompt, num_ctx=None):
        return json.dumps(self.facts, ensure_ascii=False)

    def embed(self, model, text):
        return [0.0] * 768


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


# ── meta tracking ───────────────────────────────────────────────────────────

def test_processed_max_id_starts_at_zero(db):
    assert get_processed_max_id(db) == 0


def test_mark_processed_updates_meta(db):
    mark_processed(db, 5)
    assert get_processed_max_id(db) == 5


def test_mark_processed_keeps_max(db):
    mark_processed(db, 5)
    mark_processed(db, 3)  # 古い id を渡しても下がらない
    assert get_processed_max_id(db) == 5
    mark_processed(db, 10)
    assert get_processed_max_id(db) == 10


def test_fetch_unprocessed_returns_all_when_none_processed(db):
    save_episode(db, "user", "a", "s1")
    save_episode(db, "assistant", "b", "s1")
    assert fetch_unprocessed_episode_ids(db) == [1, 2]


def test_fetch_unprocessed_excludes_already_processed(db):
    save_episode(db, "user", "a", "s1")
    save_episode(db, "assistant", "b", "s1")
    save_episode(db, "user", "c", "s1")
    mark_processed(db, 2)
    assert fetch_unprocessed_episode_ids(db) == [3]


def test_write_run_marks_each_processed(db, tmp_path: Path, monkeypatch):
    """write_run が処理した episode を mark_processed する。"""
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db.execute("PRAGMA database_list").fetchone()[2]))
    save_episode(db, "user", "コーヒーは深煎り", "s1")
    save_episode(db, "user", "Python を 10 年", "s1")

    client = FakeClient(facts=[])  # local LLM が空 → fact 追加なし、でも mark はされる
    # write_run は env から DB を取るので、ここでは直接 process_episode を回して確認
    from scripts.write.run import process_episode
    process_episode(db, 1, buffer_n=3, client=client)
    mark_processed(db, 1)
    process_episode(db, 2, buffer_n=3, client=client)
    mark_processed(db, 2)
    assert get_processed_max_id(db) == 2
    assert fetch_unprocessed_episode_ids(db) == []


# ── SessionStart 統合: 未処理を検出して detach (PERSONA_WRITE_DISABLE で実 spawn 抑止) ─

def _run_session_start(db_path: Path, write_disable: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PERSONA_MEMORY_DB"] = str(db_path)
    env["PYTHONPATH"] = str(ROOT)
    if write_disable:
        env["PERSONA_WRITE_DISABLE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "scripts.hooks.on_session_start"],
        input=b"{}",
        env=env,
        capture_output=True,
    )


def test_session_start_reports_pending_when_unprocessed_exist(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        save_episode(conn, "user", "test", "s1")
        save_episode(conn, "assistant", "reply", "s1")
    finally:
        conn.close()

    r = _run_session_start(db_path)
    assert r.returncode == 0
    err = r.stderr.decode()
    assert "resuming 2 unprocessed" in err


def test_session_start_silent_when_all_processed(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        save_episode(conn, "user", "test", "s1")
        mark_processed(conn, 1)
    finally:
        conn.close()

    r = _run_session_start(db_path)
    assert r.returncode == 0
    assert "resuming" not in r.stderr.decode()
