"""反省モード state 管理 (scripts.reflection.state) のテスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db_cozo.connection import init_db
from scripts.reflection.state import (
    clear, enter, get_state, increment_turn,
    is_detection_enabled, set_detection_enabled,
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


def test_state_stays_active_no_matter_how_many_turns(client):
    """0.8.6 仕様: 何ターン経っても自動解除はしない. 解除はご主人様の意思のみ."""
    enter(client, episode_id=1, anger_phrase="x")
    for _ in range(100):
        increment_turn(client)
    assert get_state(client).active is True
    assert get_state(client).turn_count == 101


def test_should_force_clear_is_removed():
    """20 ターン強制解除のヘルパは仕様改修で削除されている."""
    from scripts.reflection import state as state_mod
    assert not hasattr(state_mod, "should_force_clear")
    assert not hasattr(state_mod, "MAX_TURNS")


def test_detection_enabled_default_true(client):
    """新規 DB では検知は有効扱い (= default on)."""
    assert is_detection_enabled(client) is True


def test_detection_enabled_toggle(client):
    set_detection_enabled(client, False)
    assert is_detection_enabled(client) is False
    set_detection_enabled(client, True)
    assert is_detection_enabled(client) is True


def test_detection_flag_survives_clear(client):
    """clear() は反省 state のみ解除. detection_enabled は別フラグなので残る."""
    set_detection_enabled(client, False)
    enter(client, episode_id=1, anger_phrase="x")
    clear(client)
    # active は消えるが detection_enabled は維持
    assert get_state(client).active is False
    assert is_detection_enabled(client) is False
