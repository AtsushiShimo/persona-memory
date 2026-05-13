"""hook 配線 (scripts.db_cozo.wire) のテスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db.connection import connect as sql_connect
from scripts.db.migrate import init_db as sql_init
from scripts.db_cozo.connection import init_db as cozo_init
from scripts.db_cozo.discussion import add_edge, add_node
from scripts.db_cozo.repo import (
    ensure_topic, save_episode as cozo_save, set_active_topic,
)
from scripts.db_cozo.wire import (
    cozo_db_path_for, cozo_db_present, cozo_disabled,
    maybe_cozo_recall_block, maybe_cozo_save_episode,
)


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    p = tmp_path / "p.db"
    sql_init(p)
    return p


def test_cozo_db_path_for_replaces_suffix(tmp_path: Path):
    src = tmp_path / "ソフィア.db"
    out = cozo_db_path_for(src)
    assert out.name == "ソフィア.cozo.db"


def test_cozo_db_present_false_when_missing(sqlite_path: Path):
    assert not cozo_db_present(sqlite_path)


def test_cozo_db_present_true_when_file_exists(sqlite_path: Path):
    cozo_init(cozo_db_path_for(sqlite_path))
    assert cozo_db_present(sqlite_path)


def test_cozo_disabled_returns_true_when_env_set(monkeypatch):
    monkeypatch.setenv("PERSONA_COZO_DISABLE", "1")
    assert cozo_disabled()


def test_cozo_disabled_returns_false_by_default(monkeypatch):
    monkeypatch.delenv("PERSONA_COZO_DISABLE", raising=False)
    assert not cozo_disabled()


def test_maybe_cozo_save_episode_noop_when_db_absent(sqlite_path: Path):
    out = maybe_cozo_save_episode(sqlite_path, "user", "x", "s1")
    assert out is None


def test_maybe_cozo_save_episode_saves_when_db_present(sqlite_path: Path):
    cozo_init(cozo_db_path_for(sqlite_path))
    eid = maybe_cozo_save_episode(sqlite_path, "user", "hello", "s1")
    assert isinstance(eid, int) and eid > 0


def test_maybe_cozo_save_episode_noop_when_disabled(sqlite_path: Path, monkeypatch):
    cozo_init(cozo_db_path_for(sqlite_path))
    monkeypatch.setenv("PERSONA_COZO_DISABLE", "1")
    assert maybe_cozo_save_episode(sqlite_path, "user", "x", "s1") is None


def test_maybe_cozo_recall_block_noop_when_db_absent(sqlite_path: Path):
    assert maybe_cozo_recall_block(sqlite_path, "Renju") == ""
