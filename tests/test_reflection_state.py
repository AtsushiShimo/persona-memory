"""反省モード state 管理 (scripts.reflection.state) のテスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db_cozo.connection import init_db
from scripts.reflection.state import (
    MAX_TURNS, clear, enter, get_state, increment_turn, should_force_clear,
)


@pytest.fixture
def client(tmp_path: Path):
    return init_db(tmp_path / "p.cozo.db")


def test_initial_state_is_inactive(client):
    state = get_state(client)
    assert state.active is False
    assert state.turn_count == 0


def test_enter_marks_active(client):
    enter(client, episode_id=42, anger_phrase="ちげー")
    state = get_state(client)
    assert state.active is True
    assert state.trigger_episode_id == 42
    assert state.anger_phrase == "ちげー"
    assert state.turn_count == 1


def test_enter_when_already_active_increments_turn(client):
    enter(client, episode_id=42, anger_phrase="ちげー")
    enter(client, episode_id=99, anger_phrase="やめろ")  # 2 回目
    state = get_state(client)
    assert state.active is True
    # 突入時の episode_id / phrase は最初のものを保持
    assert state.trigger_episode_id == 42
    assert state.anger_phrase == "ちげー"
    assert state.turn_count == 2


def test_increment_turn_returns_new_count(client):
    enter(client, episode_id=1, anger_phrase="x")
    tc = increment_turn(client)
    assert tc == 2
    tc = increment_turn(client)
    assert tc == 3


def test_increment_turn_inactive_returns_zero(client):
    assert increment_turn(client) == 0


def test_clear_resets_state(client):
    enter(client, episode_id=1, anger_phrase="x")
    clear(client)
    state = get_state(client)
    assert state.active is False
    assert state.turn_count == 0


def test_force_clear_threshold(client, monkeypatch):
    enter(client, episode_id=1, anger_phrase="x")
    # MAX_TURNS まで増やす
    from scripts.reflection import state as state_mod
    for _ in range(state_mod.MAX_TURNS - 1):
        increment_turn(client)
    assert should_force_clear(client) is True


def test_force_clear_under_threshold(client):
    enter(client, episode_id=1, anger_phrase="x")
    assert should_force_clear(client) is False
