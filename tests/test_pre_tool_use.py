"""PreToolUse hook で persona-memory DB 直接アクセスをブロックするテスト."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _run_hook(payload: dict) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    r = subprocess.run(
        [PYTHON, "-m", "scripts.hooks.on_pre_tool_use"],
        input=json.dumps(payload).encode(),
        env=env,
        capture_output=True,
    )
    return r.returncode, r.stdout.decode(), r.stderr.decode()


def _is_denied(stdout: str) -> bool:
    if not stdout.strip():
        return False
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    return (
        data.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    )


# ── ブロックすべきケース ────────────────────────────────────────────────────

def test_block_sqlite3_persona_db():
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "sqlite3 .persona-memory/凜.db 'SELECT * FROM facts'"},
    })
    assert rc == 0
    assert _is_denied(out)


def test_block_sqlite3_with_full_path():
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "sqlite3 /home/user/proj/.persona-memory/test.db .schema"},
    })
    assert _is_denied(out)


def test_block_cat_persona_db():
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "cat .persona-memory/test.db | head"},
    })
    assert _is_denied(out)


def test_block_strings_persona_db():
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "strings .persona-memory/foo.db"},
    })
    assert _is_denied(out)


def test_block_read_tool_on_db_file():
    rc, out, _ = _run_hook({
        "tool_name": "Read",
        "tool_input": {"file_path": "/home/user/proj/.persona-memory/test.db"},
    })
    assert _is_denied(out)


# ── 通すべきケース (false positive 防止) ────────────────────────────────────

def test_allow_normal_bash():
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
    })
    assert rc == 0
    assert out.strip() == ""


def test_allow_git_command():
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "git log --oneline | head"},
    })
    assert out.strip() == ""


def test_allow_sqlite3_other_db():
    """persona-memory 以外の DB は触っても OK."""
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "sqlite3 /tmp/other.db 'SELECT 1'"},
    })
    assert out.strip() == ""


def test_allow_persona_memory_dir_listing():
    """ls .persona-memory/ は OK (ディレクトリ確認、DB 中身は読まない)."""
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la .persona-memory/"},
    })
    assert out.strip() == ""


def test_allow_read_other_files():
    rc, out, _ = _run_hook({
        "tool_name": "Read",
        "tool_input": {"file_path": "/etc/hosts"},
    })
    assert out.strip() == ""


def test_allow_read_config_env():
    """config.env (テキスト) は読んで OK、DB のみブロック."""
    rc, out, _ = _run_hook({
        "tool_name": "Read",
        "tool_input": {"file_path": "/proj/.persona-memory/凜.config.env"},
    })
    assert out.strip() == ""


def test_other_tool_passthrough():
    """Bash / Read 以外のツールは何もしない."""
    rc, out, _ = _run_hook({
        "tool_name": "Edit",
        "tool_input": {"file_path": "anything"},
    })
    assert out.strip() == ""


# ── false positive 防止 (regex が緩すぎて自身のコミットを止めた回帰テスト) ──

def test_allow_git_commit_message_mentioning_sqlite_and_persona_memory():
    """git commit -m '...sqlite3...persona-memory...db...' は通す.

    実際に 0.4.14 リリースで自分のコミットがブロックされた事象の回帰テスト。
    subcommand の **先頭が** sqlite3 等でない限りブロックしない方針。
    """
    msg = "scripts/hooks に sqlite3 で .persona-memory/foo.db を読むのを禁止する hook を追加"
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": f"git commit -m '{msg}'"},
    })
    assert out.strip() == ""


def test_allow_echo_with_dangerous_strings():
    """echo の引数に sqlite3/.persona-memory/.db が含まれても、echo は危険ツールでない."""
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "echo 'sqlite3 .persona-memory/foo.db'"},
    })
    assert out.strip() == ""


def test_block_chained_sqlite3():
    """`cd ~ && sqlite3 .persona-memory/foo.db` のような連結はブロック."""
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "cd ~ && sqlite3 .persona-memory/foo.db .schema"},
    })
    assert _is_denied(out)


def test_block_env_prefix_sqlite3():
    """env var 前置きでも sqlite3 自体が先頭ならブロック."""
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "DEBUG=1 sqlite3 .persona-memory/foo.db .schema"},
    })
    assert _is_denied(out)
