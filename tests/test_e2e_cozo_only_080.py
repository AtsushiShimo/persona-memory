"""0.8.0 Cozo 単独経路の e2e 自己テスト.

server/db.py が全機能 Cozo backend で動くこと, hook 経路が SQLite 不在でも
動くことを、 一時 DB + dummy embedding で網羅確認.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest


def _dummy_emb() -> bytes:
    return struct.pack(f"{768}f", *([0.1] * 768))


@pytest.fixture
def fresh_persona(tmp_path: Path, monkeypatch):
    """一時 Cozo DB + active-persona を準備."""
    pdir = tmp_path / ".persona-memory"
    pdir.mkdir()
    (pdir / "active-persona").write_text("t", encoding="utf-8")
    sqlite_path = pdir / "t.db"
    sqlite_path.touch()
    cozo_path = pdir / "t.cozo.db"
    from scripts.db_cozo.connection import init_db
    init_db(cozo_path)
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(sqlite_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    return {"sqlite": sqlite_path, "cozo": cozo_path}


def test_server_db_upsert_search_delete_cycle(fresh_persona):
    """write_fact → search → delete の往復が Cozo only で動く."""
    from server import db
    # 通常 category
    fid = db.upsert_fact(
        category="preference", key="coffee", value="深煎り",
        importance=5, source=None,
    )
    db.write_fact_embedding(fid, _dummy_emb())
    # search で見つかる
    results = db.search_facts(
        embedding_blob=_dummy_emb(), top_k=5, category=None,
    )
    assert any(r["id"] == fid and r["value"] == "深煎り" for r in results)
    # list_active_facts に出る
    actives = db.list_active_facts()
    assert any(r["id"] == fid for r in actives)
    # delete
    out = db.delete_fact(fact_id=fid)
    assert out is not None
    actives2 = db.list_active_facts()
    assert not any(r["id"] == fid for r in actives2)


def test_lesson_category_accepted_via_cozo(fresh_persona):
    """0.7.7 lesson カテゴリ + importance 10 が Cozo で通る (= SQLite CHECK 制約解消)."""
    from server import db
    fid = db.upsert_fact(
        category="lesson", key="test_lesson",
        value="テスト lesson", importance=10, source="reflection",
    )
    assert fid > 0
    actives = db.list_active_facts()
    assert any(r["id"] == fid for r in actives)


def test_append_episode_and_search(fresh_persona):
    """append_episode + search_episodes の往復が動く."""
    from server import db
    eid = db.append_episode(
        session_id="s1", role="user", content="今日の議題",
        summary="議題のメモ",
    )
    assert eid > 0
    db.write_episode_embedding(eid, _dummy_emb())
    results = db.search_episodes(embedding_blob=_dummy_emb(), top_k=5)
    assert any(r["id"] == eid for r in results)


def test_persona_name_extraction(fresh_persona):
    """get_persona_name が persona/identity の 『』 内を抽出."""
    from server import db
    db.upsert_fact(
        category="persona", key="identity",
        value="このペルソナの名前は『ルミナス』",
        importance=9, source="init",
    )
    assert db.get_persona_name() == "ルミナス"


def test_hook_user_prompt_runs_without_sqlite(fresh_persona):
    """on_user_prompt.py の core 経路が SQLite 不在でも例外せず終わる."""
    import json
    import os
    import subprocess
    import sys
    # 0.8.5 LLM-only 化以降、 怒気検知の skip は DB persisted flag に集約.
    # この e2e は LLM call なしで完走させたいので、 事前に disable を刻む.
    from scripts.db_cozo.connection import init_db
    from scripts.reflection.state import set_detection_enabled
    set_detection_enabled(init_db(fresh_persona["cozo"]), False)

    env = {
        **os.environ,
        "PYTHONPATH": ".",
        "PERSONA_TOPIC_IDENTIFY_DISABLE": "1",
        "PERSONA_TOPIC_DISABLE": "1",
        "PERSONA_RECALL_DISABLE": "1",
        "PERSONA_WRITE_DISABLE": "1",
        "PERSONA_MEMORY_DB": str(fresh_persona["sqlite"]),
        "CLAUDE_PROJECT_DIR": str(fresh_persona["sqlite"].parent.parent),
    }
    payload = json.dumps({"prompt": "おはよう", "session_id": "s_e2e"})
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.hooks.on_user_prompt"],
        input=payload, text=True, capture_output=True,
        env=env, timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"


def test_lesson_register_and_match_through_mcp_path(fresh_persona):
    """0.7.7 register_lesson_triggers 経由で lesson + trigger が刻まれる."""
    from server import db
    # まず lesson 本体を upsert
    db.upsert_fact(
        category="lesson", key="test_block",
        value="test block lesson", importance=10, source="reflection",
    )
    # trigger 登録 (MCP tool 相当の経路を直接)
    from scripts.db_cozo.connection import init_db
    from scripts.reflection.lesson import (
        get_lesson_by_key, register_trigger, match_lessons_for_tool_call,
    )
    client = init_db(fresh_persona["cozo"])
    lesson = get_lesson_by_key(client, "test_block")
    assert lesson is not None
    register_trigger(
        client, lesson.fact_id, "path_edit", r"/forbidden/", "block",
    )
    # マッチ確認
    matches = match_lessons_for_tool_call(
        client, "Edit", {"file_path": "/forbidden/x.py"},
    )
    assert any(m.lesson.key == "test_block" for m in matches)


def test_debug_mode_toggle_via_mcp_path(fresh_persona):
    """set_debug_mode tool 経路で flag が立つ + hook が読める."""
    from scripts.debug.mode import enable, is_active, disable
    enable(fresh_persona["sqlite"], ttl_seconds=60, reason="test")
    assert is_active(fresh_persona["sqlite"]) is True
    disable(fresh_persona["sqlite"])
    assert is_active(fresh_persona["sqlite"]) is False


def test_health_check_returns_cozo_db_info(fresh_persona):
    """health.collect が Cozo path を見て ok を返す."""
    from scripts.health import collect
    result = collect(fresh_persona["sqlite"])
    assert isinstance(result, dict)
    assert "db" in result
    assert result["db"]["ok"] is True
