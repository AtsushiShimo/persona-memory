"""OLLAMA_NUM_PARALLEL 自動設定のテスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.tools.configure_ollama_parallel import (
    END_MARKER,
    START_MARKER,
    recommend_parallel,
    write_block,
)


@pytest.mark.parametrize("ram_gb,expected", [
    (4, 2),
    (8, 2),
    (11, 2),
    (12, 4),
    (16, 4),
    (23, 4),
    (24, 6),
    (32, 6),
    (47, 6),
    (48, 8),
    (64, 8),
    (128, 8),
])
def test_recommend_parallel(ram_gb, expected):
    assert recommend_parallel(ram_gb) == expected


def test_write_block_creates_new_file(tmp_path: Path):
    rc = tmp_path / ".zshrc"
    status = write_block(rc, 4)
    assert status == "created"
    text = rc.read_text()
    assert START_MARKER in text
    assert END_MARKER in text
    assert "export OLLAMA_NUM_PARALLEL=4" in text


def test_write_block_appends_to_existing(tmp_path: Path):
    rc = tmp_path / ".zshrc"
    rc.write_text("export EDITOR=vim\n")
    status = write_block(rc, 6)
    assert status == "appended"
    text = rc.read_text()
    assert "export EDITOR=vim" in text  # 既存設定保持
    assert "export OLLAMA_NUM_PARALLEL=6" in text


def test_write_block_updates_existing_block(tmp_path: Path):
    rc = tmp_path / ".zshrc"
    # 古い値のブロックを仕込む
    old_block = f"{START_MARKER}\nexport OLLAMA_NUM_PARALLEL=2\n{END_MARKER}\n"
    rc.write_text(f"export USER_VAR=keep\n\n{old_block}")
    status = write_block(rc, 8)
    assert status == "updated"
    text = rc.read_text()
    assert "export USER_VAR=keep" in text
    assert "OLLAMA_NUM_PARALLEL=2" not in text
    assert "OLLAMA_NUM_PARALLEL=8" in text
    # マーカーは 1 セットのみ
    assert text.count(START_MARKER) == 1
    assert text.count(END_MARKER) == 1


def test_write_block_idempotent(tmp_path: Path):
    rc = tmp_path / ".zshrc"
    write_block(rc, 4)
    status = write_block(rc, 4)
    assert status == "unchanged"
    text = rc.read_text()
    assert text.count(START_MARKER) == 1


def test_write_block_only_touches_marked_region(tmp_path: Path):
    """マーカー外のユーザー設定は触らない."""
    rc = tmp_path / ".zshrc"
    user_content = (
        "export PATH=/custom/bin:$PATH\n"
        "alias ll='ls -la'\n"
        "# my comment\n"
    )
    rc.write_text(user_content)
    write_block(rc, 6)
    text = rc.read_text()
    # 元の設定はそのまま残っている
    assert "export PATH=/custom/bin:$PATH" in text
    assert "alias ll='ls -la'" in text
    assert "# my comment" in text
    # 新しいブロックも追加されている
    assert "export OLLAMA_NUM_PARALLEL=6" in text
