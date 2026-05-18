"""緊急停止経路 (scripts.reflection.emergency / MCP set_anger_detection) の smoke."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.db_cozo.connection import init_db
from scripts.reflection.state import (
    enter, is_detection_enabled, set_detection_enabled,
)


@pytest.fixture
def fresh_db(tmp_path: Path):
    """空の Cozo DB を作って path 一式を返す."""
    persona_dir = tmp_path / ".persona-memory"
    persona_dir.mkdir()
    (persona_dir / "active-persona").write_text("test", encoding="utf-8")
    sqlite_path = persona_dir / "test.db"
    cozo_path = persona_dir / "test.cozo.db"
    sqlite_path.touch()
    init_db(cozo_path)
    return {"sqlite": sqlite_path, "cozo": cozo_path, "root": tmp_path}


def _run_emergency(db_path: Path, action: str) -> dict:
    env = {**os.environ, "PYTHONPATH": ".", "PERSONA_MEMORY_DB": str(db_path)}
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.reflection.emergency", action],
        text=True, capture_output=True, env=env, timeout=15,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_emergency_off_disables_detection(fresh_db):
    out = _run_emergency(fresh_db["sqlite"], "off")
    assert out["ok"] is True
    assert out["detection_enabled"] is False
    client = init_db(fresh_db["cozo"])
    assert is_detection_enabled(client) is False


def test_emergency_off_clears_active_state(fresh_db):
    client = init_db(fresh_db["cozo"])
    enter(client, episode_id=1, anger_phrase="x")
    out = _run_emergency(fresh_db["sqlite"], "off")
    assert out["reflection_state_cleared"] is True
    # 反省 state は解除されている
    from scripts.reflection.state import get_state
    assert get_state(client).active is False


def test_emergency_on_reenables(fresh_db):
    _run_emergency(fresh_db["sqlite"], "off")
    out = _run_emergency(fresh_db["sqlite"], "on")
    assert out["ok"] is True
    assert out["detection_enabled"] is True
    client = init_db(fresh_db["cozo"])
    assert is_detection_enabled(client) is True


def test_emergency_status_reflects_current(fresh_db):
    client = init_db(fresh_db["cozo"])
    set_detection_enabled(client, False)
    out = _run_emergency(fresh_db["sqlite"], "status")
    assert out["detection_enabled"] is False
    assert out["reflection_active"] is False


def test_mcp_set_anger_detection_off_path(fresh_db, monkeypatch):
    """MCP tool 経由でも同じ動作 (= 入口 2 種で挙動が一致)."""
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(fresh_db["sqlite"]))
    client = init_db(fresh_db["cozo"])
    enter(client, episode_id=7, anger_phrase="y")
    # MCP tool 本体は FastMCP wrapper. wrapper 越しではなく中身を呼ぶ.
    from server import main as server_main
    # FastMCP は @mcp.tool() で wrap するので fn 本体を取り出す.
    tool = server_main.set_anger_detection
    # FunctionTool 化されている場合は .fn を見る
    raw = getattr(tool, "fn", tool)
    result = raw(enabled=False, clear_state=True)
    assert result["ok"] is True
    assert result["detection_enabled"] is False
    assert result["reflection_state_cleared"] is True


def test_mcp_set_anger_detection_on_path(fresh_db, monkeypatch):
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(fresh_db["sqlite"]))
    from server import main as server_main
    raw = getattr(server_main.set_anger_detection, "fn", server_main.set_anger_detection)
    result = raw(enabled=True)
    assert result["ok"] is True
    assert result["detection_enabled"] is True
    # active state は無いので reflection_state_cleared は False
    assert result["reflection_state_cleared"] is False
