"""PreToolUse hook で persona-memory DB 直接アクセスをブロックするテスト."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _run_hook(payload: dict, debug: bool = False) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env.pop("PERSONA_MEMORY_DEBUG", None)
    if debug:
        env["PERSONA_MEMORY_DEBUG"] = "c"
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


def _denial_reason(stdout: str) -> str:
    try:
        data = json.loads(stdout)
        return data.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
    except json.JSONDecodeError:
        return ""


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


# ── Claude Code auto-memory ブロック ────────────────────────────────────────

def test_block_write_to_auto_memory():
    rc, out, _ = _run_hook({
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/Users/x/.claude/projects/-Users-x-proj/memory/notes.md",
            "content": "...",
        },
    })
    assert _is_denied(out)
    assert "auto-memory" in _denial_reason(out)


def test_block_edit_in_auto_memory():
    rc, out, _ = _run_hook({
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "/Users/x/.claude/projects/-Users-x-proj/memory/MEMORY.md",
            "old_string": "x", "new_string": "y",
        },
    })
    assert _is_denied(out)


def test_block_multiedit_in_auto_memory():
    rc, out, _ = _run_hook({
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": "/Users/x/.claude/projects/-Users-x-proj/memory/foo.md",
            "edits": [],
        },
    })
    assert _is_denied(out)


def test_block_read_from_auto_memory():
    rc, out, _ = _run_hook({
        "tool_name": "Read",
        "tool_input": {
            "file_path": "/Users/x/.claude/projects/-Users-x-proj/memory/MEMORY.md",
        },
    })
    assert _is_denied(out)
    assert "auto-memory" in _denial_reason(out)


def test_block_bash_write_to_auto_memory():
    """`echo ... >> ~/.claude/projects/.../memory/...` も止める."""
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {
            "command": "echo 'note' >> ~/.claude/projects/-Users-x-proj/memory/note.md",
        },
    })
    assert _is_denied(out)


def test_block_bash_mkdir_in_auto_memory():
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {
            "command": "mkdir -p ~/.claude/projects/-Users-x-proj/memory/notes",
        },
    })
    assert _is_denied(out)


# ── auto-memory 関連の false positive 防止 ──────────────────────────────────

def test_allow_write_to_normal_md():
    rc, out, _ = _run_hook({
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/Users/x/some/proj/README.md",
            "content": "...",
        },
    })
    assert out.strip() == ""


def test_allow_read_outside_auto_memory():
    """.claude/projects/ 配下でも memory/ ではないファイルは OK."""
    rc, out, _ = _run_hook({
        "tool_name": "Read",
        "tool_input": {
            "file_path": "/Users/x/.claude/projects/-Users-x-proj/sessions/abc.jsonl",
        },
    })
    assert out.strip() == ""


def test_allow_string_mention_of_auto_memory_in_commit():
    """git commit でメッセージ内に auto-memory を言及するのは OK."""
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {
            "command": "git commit -m 'block writes to ~/.claude/projects/x/memory'",
        },
    })
    assert out.strip() == ""


def test_allow_complex_commit_message_with_mkdir_and_path():
    """0.4.17 のコミットでブロックされた回帰: コミットメッセージに mkdir と
    .claude/projects/.../memory/ 文字列が両方含まれていても通す."""
    msg = (
        "release: 0.4.17\n"
        "- Bash で >>, tee, mkdir で auto-memory に書く workaround も deny\n"
        "- 経路: ~/.claude/projects/<project>/memory/ 配下"
    )
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": f"git commit -m \"{msg}\""},
    })
    assert out.strip() == "", f"unexpectedly blocked: {out}"


# ── PERSONA_MEMORY_DEBUG bypass (DB block のみ解除、 auto-memory block は維持) ──

def test_debug_mode_bypasses_sqlite3_block():
    """PERSONA_MEMORY_DEBUG が set されていれば DB 直接アクセスは通る."""
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "sqlite3 .persona-memory/foo.db 'SELECT 1'"},
    }, debug=True)
    assert out.strip() == "", f"debug mode should bypass DB block: {out}"


def test_debug_mode_bypasses_cat_db_block():
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {"command": "cat .persona-memory/foo.db | head"},
    }, debug=True)
    assert out.strip() == ""


def test_debug_mode_bypasses_read_db_block():
    rc, out, _ = _run_hook({
        "tool_name": "Read",
        "tool_input": {"file_path": "/proj/.persona-memory/foo.db"},
    }, debug=True)
    assert out.strip() == ""


def test_debug_mode_does_not_bypass_auto_memory_block():
    """auto-memory block は debug mode でも維持される (記憶分散防止の独立保護)."""
    rc, out, _ = _run_hook({
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/Users/x/.claude/projects/-Users-x-proj/memory/notes.md",
            "content": "...",
        },
    }, debug=True)
    assert _is_denied(out), "auto-memory block should stay even in debug mode"
    assert "auto-memory" in _denial_reason(out)


def test_debug_mode_does_not_bypass_bash_auto_memory_block():
    rc, out, _ = _run_hook({
        "tool_name": "Bash",
        "tool_input": {
            "command": "echo 'x' >> ~/.claude/projects/-Users-x-proj/memory/n.md",
        },
    }, debug=True)
    assert _is_denied(out)


def test_empty_debug_env_does_not_bypass():
    """PERSONA_MEMORY_DEBUG='' (空文字) は debug mode 扱いしない."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PERSONA_MEMORY_DEBUG"] = ""
    r = subprocess.run(
        [PYTHON, "-m", "scripts.hooks.on_pre_tool_use"],
        input=json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "sqlite3 .persona-memory/foo.db .schema"},
        }).encode(),
        env=env,
        capture_output=True,
    )
    assert _is_denied(r.stdout.decode())
