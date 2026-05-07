"""envrc_manage の add/remove 単体テスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.envrc_manage import (
    END_MARKER,
    FULL_BLOCK,
    START_MARKER,
    add_block,
    remove_block,
)


def test_add_creates_new_file(tmp_path: Path):
    envrc = tmp_path / ".envrc"
    assert add_block(envrc) == "created"
    assert envrc.exists()
    text = envrc.read_text()
    assert START_MARKER in text
    assert END_MARKER in text
    assert "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX" in text


def test_add_appends_to_existing_without_markers(tmp_path: Path):
    envrc = tmp_path / ".envrc"
    envrc.write_text("export FOO=bar\n")
    assert add_block(envrc) == "appended"
    text = envrc.read_text()
    assert "export FOO=bar" in text
    assert START_MARKER in text


def test_add_idempotent(tmp_path: Path):
    envrc = tmp_path / ".envrc"
    add_block(envrc)
    assert add_block(envrc) == "unchanged"
    # ファイル内容が壊れていない
    text = envrc.read_text()
    assert text.count(START_MARKER) == 1
    assert text.count(END_MARKER) == 1


def test_add_replaces_old_block(tmp_path: Path):
    envrc = tmp_path / ".envrc"
    # 古いブロックっぽい内容を仕込む (内容が新仕様と違う想定)
    old_block = (
        f"{START_MARKER}\n"
        f"export OLD_VAR=old_value\n"
        f"{END_MARKER}\n"
    )
    envrc.write_text(f"export USER_VAR=keep\n\n{old_block}")
    result = add_block(envrc)
    assert result == "updated"
    text = envrc.read_text()
    assert "export USER_VAR=keep" in text
    assert "OLD_VAR" not in text
    assert "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX" in text


def test_remove_strips_only_block(tmp_path: Path):
    envrc = tmp_path / ".envrc"
    envrc.write_text(f"export USER_VAR=keep\n\n{FULL_BLOCK}export AFTER=ok\n")
    result = remove_block(envrc)
    assert result == "removed"
    text = envrc.read_text()
    assert "USER_VAR=keep" in text
    assert "AFTER=ok" in text
    assert START_MARKER not in text
    assert END_MARKER not in text


def test_remove_deletes_file_if_only_block(tmp_path: Path):
    envrc = tmp_path / ".envrc"
    envrc.write_text(FULL_BLOCK)
    result = remove_block(envrc)
    assert result == "file_deleted"
    assert not envrc.exists()


def test_remove_absent(tmp_path: Path):
    envrc = tmp_path / ".envrc"
    # ファイルなし
    assert remove_block(envrc) == "absent"

    # ファイルあるがブロックなし
    envrc.write_text("export FOO=bar\n")
    assert remove_block(envrc) == "absent"
    assert envrc.read_text() == "export FOO=bar\n"


def test_block_uses_active_persona_file(tmp_path: Path):
    """生成されたブロックが .persona-memory/active-persona を参照している."""
    envrc = tmp_path / ".envrc"
    add_block(envrc)
    text = envrc.read_text()
    assert ".persona-memory/active-persona" in text
    assert "tr -d" in text  # 改行除去のディフェンス
