"""Phase 2 hook entry points (UserPromptSubmit / Stop) の統合テスト。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _run_hook(script: str, payload: dict, db_path: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PERSONA_MEMORY_DB"] = str(db_path)
    env["PYTHONPATH"] = str(ROOT)
    env["PERSONA_WRITE_DISABLE"] = "1"  # phase 3 spawn を無効化
    return subprocess.run(
        [PYTHON, str(ROOT / "scripts" / "hooks" / script)],
        input=json.dumps(payload).encode(),
        env=env,
        capture_output=True,
    )


# ── UserPromptSubmit ─────────────────────────────────────────────────────────

def test_user_prompt_saves_episode(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)

    r = _run_hook("on_user_prompt.py", {"prompt": "hi", "session_id": "s1"}, db_path)
    assert r.returncode == 0, r.stderr.decode()

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT role, content, session_id FROM episodes").fetchall()
    finally:
        conn.close()
    assert rows == [("user", "hi", "s1")]


def test_user_prompt_blocks_secret(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)

    secret = "sk-proj-" + "x" * 40
    r = _run_hook("on_user_prompt.py", {"prompt": f"key: {secret}", "session_id": "s1"}, db_path)
    # exit 2 = block (Anthropic にも送らない)
    assert r.returncode == 2
    assert b"\xe6\xa9\x9f\xe5\xaf\x86" in r.stderr or "機密".encode() in r.stderr

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
    finally:
        conn.close()
    assert rows[0] == 0  # 永続化されていない


def test_user_prompt_no_db_initialized(tmp_path: Path):
    """DB 未初期化なら no-op で exit 0 (fail-open)。"""
    missing = tmp_path / "nope.db"
    r = _run_hook("on_user_prompt.py", {"prompt": "hi", "session_id": "s1"}, missing)
    assert r.returncode == 0  # fail-open


# ── Stop ─────────────────────────────────────────────────────────────────────

def _make_transcript(path: Path, msgs: list[tuple[str, str]]) -> None:
    """role/content のリストを Claude Code JSONL 形式で書き出す。"""
    lines = []
    for role, content in msgs:
        line = {
            "type": role,
            "message": {"role": role, "content": [{"type": "text", "text": content}]},
        }
        lines.append(json.dumps(line, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_stop_saves_assistant_turn(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    transcript = tmp_path / "t.jsonl"
    _make_transcript(transcript, [("user", "hi"), ("assistant", "hello!")])

    r = _run_hook(
        "on_stop.py",
        {"transcript_path": str(transcript), "session_id": "s1"},
        db_path,
    )
    assert r.returncode == 0, r.stderr.decode()

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT role, content FROM episodes ORDER BY id").fetchall()
    finally:
        conn.close()
    assert ("user", "hi") in rows
    assert ("assistant", "hello!") in rows


def test_stop_skips_secret_turn(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    transcript = tmp_path / "t.jsonl"
    secret = "sk-proj-" + "y" * 40
    _make_transcript(
        transcript,
        [("user", "ok"), ("assistant", f"my key is {secret}")],
    )

    r = _run_hook(
        "on_stop.py",
        {"transcript_path": str(transcript), "session_id": "s1"},
        db_path,
    )
    assert r.returncode == 0

    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT role, content FROM episodes ORDER BY id").fetchall()
    finally:
        conn.close()
    assert ("user", "ok") in rows
    # secret を含む assistant ターンは保存されない
    assert all(secret not in c for _, c in rows)


def test_stop_idempotent_resume(tmp_path: Path):
    """同じ transcript で 2 回 Stop が走っても、新規ターンだけ追記される。"""
    db_path = tmp_path / "p.db"
    init_db(db_path)
    transcript = tmp_path / "t.jsonl"
    _make_transcript(transcript, [("user", "hi"), ("assistant", "hello")])

    payload = {"transcript_path": str(transcript), "session_id": "s1"}
    _run_hook("on_stop.py", payload, db_path)
    _run_hook("on_stop.py", payload, db_path)  # 同じ内容で再実行

    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    finally:
        conn.close()
    assert n == 2  # 重複しない

    # 1 ターン追記して 3 回目
    _make_transcript(
        transcript,
        [("user", "hi"), ("assistant", "hello"), ("user", "more")],
    )
    _run_hook("on_stop.py", payload, db_path)

    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    finally:
        conn.close()
    assert n == 3
