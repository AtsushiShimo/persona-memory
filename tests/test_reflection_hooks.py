"""hook 経路 (UserPromptSubmit / PreToolUse) の反省モード配線テスト.

実 LLM を呼ばないため LIGHT_MODEL を含む env を fake にして
detect_anger / lesson trigger / state を統合確認する.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _ollama_reachable() -> bool:
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=2)
        return True
    except (urllib.error.URLError, OSError, ConnectionError):
        return False


@pytest.fixture
def tmp_persona(tmp_path: Path, monkeypatch):
    """一時 DB 環境. SQLite + Cozo の両 schema を作って active-persona を設定."""
    persona_dir = tmp_path / ".persona-memory"
    persona_dir.mkdir()
    (persona_dir / "active-persona").write_text("test", encoding="utf-8")
    db_path = persona_dir / "test.db"
    cozo_path = persona_dir / "test.cozo.db"

    # 0.8.0: SQLite 経路は廃止. Cozo schema のみ. db_path は touch のみで
    # path 互換 (= get_db_path が見るファイル) として確保.
    db_path.touch()
    from scripts.db_cozo.connection import init_db as _cozo_init
    _cozo_init(cozo_path)

    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db_path))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    # 0.8.5: LLM-only 検知になったため keyword bypass フラグは消滅.
    # hook 統合テストは実 Ollama を要求する (= ローカル開発前提).
    monkeypatch.setenv("PERSONA_TOPIC_IDENTIFY_DISABLE", "1")
    monkeypatch.setenv("PERSONA_TOPIC_DISABLE", "1")
    # recall も off (= 通常発話で gemma3:12b summarize が 60s 超え timeout する).
    # 反省モード hook 経路の検証だけが目的なので recall パイプラインは不要.
    monkeypatch.setenv("PERSONA_RECALL_DISABLE", "1")
    monkeypatch.setenv("PERSONA_COZO_DISABLE", "1")
    return {"db": db_path, "cozo": cozo_path, "root": tmp_path}


def test_pretool_blocks_edit_during_reflection(tmp_persona):
    """0.8.6: 反省モード active 中は Edit ツールが block される."""
    from scripts.db_cozo.connection import init_db
    from scripts.hooks.on_pre_tool_use import _check_reflection_active_block
    from scripts.reflection.state import enter
    client = init_db(tmp_persona["cozo"])
    enter(client, episode_id=1, anger_phrase="ちげー")
    reason = _check_reflection_active_block("Edit", {"file_path": "/tmp/x.py"})
    assert reason is not None
    assert "反省モード" in reason


def test_pretool_blocks_write_during_reflection(tmp_persona):
    """Write ツールも block される."""
    from scripts.db_cozo.connection import init_db
    from scripts.hooks.on_pre_tool_use import _check_reflection_active_block
    from scripts.reflection.state import enter
    client = init_db(tmp_persona["cozo"])
    enter(client, episode_id=1, anger_phrase="x")
    reason = _check_reflection_active_block("Write", {"file_path": "/tmp/x.py"})
    assert reason is not None


def test_pretool_blocks_bash_during_reflection(tmp_persona):
    """Bash ツールも block される (read-only か否かを問わず全 Bash)."""
    from scripts.db_cozo.connection import init_db
    from scripts.hooks.on_pre_tool_use import _check_reflection_active_block
    from scripts.reflection.state import enter
    client = init_db(tmp_persona["cozo"])
    enter(client, episode_id=1, anger_phrase="x")
    reason = _check_reflection_active_block("Bash", {"command": "ls"})
    assert reason is not None


def test_pretool_does_not_block_read_during_reflection(tmp_persona):
    """Read 系・確認系は反省モード中も許可 (調査・確認のため)."""
    from scripts.db_cozo.connection import init_db
    from scripts.hooks.on_pre_tool_use import _check_reflection_active_block
    from scripts.reflection.state import enter
    client = init_db(tmp_persona["cozo"])
    enter(client, episode_id=1, anger_phrase="x")
    assert _check_reflection_active_block("Read", {"file_path": "/tmp/x.py"}) is None
    assert _check_reflection_active_block("Grep", {"pattern": "x"}) is None
    assert _check_reflection_active_block("Glob", {"pattern": "*"}) is None


def test_pretool_does_not_block_when_reflection_inactive(tmp_persona):
    """反省モードが立っていなければ Edit/Write/Bash は素通り (lesson 経路に委ねる)."""
    from scripts.hooks.on_pre_tool_use import _check_reflection_active_block
    # state を立てない
    assert _check_reflection_active_block("Edit", {"file_path": "/tmp/x.py"}) is None
    assert _check_reflection_active_block("Write", {"file_path": "/tmp/x.py"}) is None
    assert _check_reflection_active_block("Bash", {"command": "ls"}) is None


def test_pretool_blocks_when_lesson_seeded(tmp_persona, monkeypatch):
    """seed 後にキャッシュ path を Edit しようとすると lesson trigger で deny."""
    from scripts.reflection.seed_past_lessons import seed_all
    seed_all(tmp_persona["cozo"])

    from scripts.hooks.on_pre_tool_use import _check_lesson_triggers
    reason = _check_lesson_triggers(
        "Edit",
        {"file_path": str(Path.home() / ".claude/plugins/cache/persona-memory/x.py")},
    )
    assert reason is not None
    assert "キャッシュ" in reason or "cache" in reason.lower() or "中止" in reason


def test_pretool_no_lesson_match_returns_none(tmp_persona):
    """seed 無し or 該当 path 無しなら None."""
    from scripts.hooks.on_pre_tool_use import _check_lesson_triggers
    reason = _check_lesson_triggers(
        "Edit", {"file_path": "/tmp/safe.py"},
    )
    assert reason is None


def test_pretool_blocks_seeded_path_via_full_main(tmp_persona):
    """on_pre_tool_use.main() の e2e 経路: stdin payload → stdout permissionDecision."""
    from scripts.reflection.seed_past_lessons import seed_all
    seed_all(tmp_persona["cozo"])

    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(Path.home() / ".claude/plugins/cache/persona-memory/x.py"),
        },
    }
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.hooks.on_pre_tool_use"],
        input=json.dumps(payload),
        text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": "."},
        timeout=30,
    )
    assert proc.returncode == 0
    out = proc.stdout.strip()
    assert out, f"no output. stderr={proc.stderr}"
    data = json.loads(out)
    assert data["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.skipif(not _ollama_reachable(),
                    reason="LLM-only 検知は実 Ollama を要求する")
def test_userprompt_injects_reflection_on_anger(tmp_persona):
    """怒気発話 → additionalContext に「反省モード」 ブロックが含まれる."""
    payload = {
        "prompt": "ちげーよ、 やり直して",
        "session_id": "test-session",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.hooks.on_user_prompt"],
        input=json.dumps(payload),
        text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": "."},
        timeout=60,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    out = proc.stdout.strip()
    assert out, f"empty output. stderr={proc.stderr}"
    data = json.loads(out)
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert "反省モード" in ctx
    assert "謝罪" in ctx


def test_userprompt_skips_detection_when_dynamically_disabled(tmp_persona):
    """detection_enabled=False を DB に persist しておくと、 怒り発話でも
    反省モードが発火しない (= LLM call も skip されるため Ollama 不要)."""
    from scripts.db_cozo.connection import init_db
    from scripts.reflection.state import set_detection_enabled
    client = init_db(tmp_persona["cozo"])
    set_detection_enabled(client, False)

    payload = {
        "prompt": "ちげーよ、 やり直して",
        "session_id": "test-session-disabled",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.hooks.on_user_prompt"],
        input=json.dumps(payload),
        text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": "."},
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    out = proc.stdout.strip()
    if out:
        data = json.loads(out)
        ctx = data["hookSpecificOutput"].get("additionalContext", "")
        assert "【反省モード発火】" not in ctx


@pytest.mark.skipif(not _ollama_reachable(),
                    reason="LLM-only 検知は実 Ollama を要求する")
def test_userprompt_no_reflection_on_normal(tmp_persona):
    """通常発話では反省モードブロックが出ない."""
    payload = {
        "prompt": "おはようございます",
        "session_id": "test-session",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.hooks.on_user_prompt"],
        input=json.dumps(payload),
        text=True, capture_output=True,
        env={**os.environ, "PYTHONPATH": "."},
        timeout=60,
    )
    assert proc.returncode == 0
    # 出力があったとしても「反省モード発火」 ブロックは含まれない
    out = proc.stdout.strip()
    if out:
        data = json.loads(out)
        ctx = data["hookSpecificOutput"].get("additionalContext", "")
        assert "【反省モード発火】" not in ctx
