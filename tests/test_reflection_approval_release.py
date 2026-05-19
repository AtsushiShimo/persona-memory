"""ご主人様承認による反省モード解除のテスト (0.8.6).

承認 = lesson 保存 + register_lesson_triggers 成功. この組み合わせが完了した
タイミングで反省 state を自動 clear する.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch):
    persona_dir = tmp_path / ".persona-memory"
    persona_dir.mkdir()
    (persona_dir / "active-persona").write_text("test", encoding="utf-8")
    db_path = persona_dir / "test.db"
    cozo_path = persona_dir / "test.cozo.db"
    db_path.touch()
    from scripts.db_cozo.connection import init_db
    init_db(cozo_path)
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db_path))
    return {"db": db_path, "cozo": cozo_path}


def _insert_lesson_fact(client, key: str, value: str = "test") -> int:
    """Cozo に直接 lesson fact を書き込む (Ollama 不要)."""
    from scripts.db_cozo.connection import next_id
    import datetime as _dt
    ts = _dt.datetime.now().isoformat(timespec="seconds")
    fact_id = next_id(client, "fact")
    client.run(
        "?[id, category, key, value, importance, status, created_at, "
        "updated_at, access_count] <- "
        "[[$id, 'lesson', $key, $value, 10, 'active', $ts, $ts, 0]] "
        ":put fact {id => category, key, value, importance, status, "
        "created_at, updated_at, access_count}",
        {"id": fact_id, "key": key, "value": value, "ts": ts},
    )
    return fact_id


def test_register_lesson_triggers_clears_active_reflection(fresh_db):
    """反省モード active + lesson 保存済 + triggers 登録成功 → state clear される."""
    from scripts.db_cozo.connection import init_db
    from scripts.reflection.state import enter, get_state
    from server import main as server_main

    client = init_db(fresh_db["cozo"])
    enter(client, episode_id=1, anger_phrase="ちげー")
    assert get_state(client).active is True

    _insert_lesson_fact(client, "test_lesson_for_release")

    raw = getattr(server_main.register_lesson_triggers, "fn",
                  server_main.register_lesson_triggers)
    res = raw(
        lesson_key="test_lesson_for_release",
        triggers=[{"kind": "general", "pattern": "x", "action": "warn"}],
    )
    assert res.get("registered", 0) >= 1, f"unexpected: {res}"
    assert get_state(client).active is False


def test_register_lesson_triggers_no_clear_if_inactive(fresh_db):
    """反省モードが立っていなければ register_lesson_triggers で何も起きない."""
    from scripts.db_cozo.connection import init_db
    from scripts.reflection.state import get_state
    from server import main as server_main

    client = init_db(fresh_db["cozo"])
    _insert_lesson_fact(client, "test_lesson_no_state")
    raw = getattr(server_main.register_lesson_triggers, "fn",
                  server_main.register_lesson_triggers)
    res = raw(
        lesson_key="test_lesson_no_state",
        triggers=[{"kind": "general", "pattern": "x", "action": "warn"}],
    )
    assert res.get("registered", 0) >= 1
    assert get_state(client).active is False


def test_register_lesson_triggers_no_clear_on_lesson_missing(fresh_db):
    """lesson が無くて失敗した時は反省 state を clear しない (承認に至らないため)."""
    from scripts.db_cozo.connection import init_db
    from scripts.reflection.state import enter, get_state
    from server import main as server_main

    client = init_db(fresh_db["cozo"])
    enter(client, episode_id=1, anger_phrase="x")
    raw = getattr(server_main.register_lesson_triggers, "fn",
                  server_main.register_lesson_triggers)
    res = raw(
        lesson_key="nonexistent_lesson",
        triggers=[{"kind": "general", "pattern": "x", "action": "warn"}],
    )
    assert "error" in res
    assert get_state(client).active is True
