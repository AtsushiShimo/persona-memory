"""デバッグモード state 管理 (scripts.debug.mode) のテスト."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from scripts.debug import mode as dm


@pytest.fixture
def db_path(tmp_path: Path):
    pdir = tmp_path / ".persona-memory"
    pdir.mkdir()
    p = pdir / "test.db"
    p.touch()
    return p


def test_initial_inactive(db_path):
    assert dm.is_active(db_path) is False
    assert dm.status(db_path)["active"] is False


def test_enable_creates_flag(db_path):
    out = dm.enable(db_path, ttl_seconds=300, reason="DB 調査")
    assert out["active"] is True
    assert out["reason"] == "DB 調査"
    assert dm.is_active(db_path) is True


def test_disable_removes_flag(db_path):
    dm.enable(db_path)
    dm.disable(db_path)
    assert dm.is_active(db_path) is False


def test_disable_when_not_active_is_noop(db_path):
    out = dm.disable(db_path)
    assert out["active"] is False
    assert out["removed"] is False


def test_expired_flag_auto_cleans(db_path):
    """expires_at が過去なら自動清掃."""
    flag = dm._flag_path(db_path)
    flag.write_text(f"expires_at:{int(time.time()) - 10}\nreason:old\n",
                    encoding="utf-8")
    assert dm.is_active(db_path) is False
    # 自己清掃で flag 消失
    assert not flag.exists()


def test_malformed_flag_returns_inactive(db_path):
    """壊れた flag (= expires_at 欠落) は inactive 扱い."""
    flag = dm._flag_path(db_path)
    flag.write_text("garbage", encoding="utf-8")
    assert dm.is_active(db_path) is False


def test_enable_extends_existing(db_path):
    """既に on の状態で enable → expires_at が更新される."""
    dm.enable(db_path, ttl_seconds=100)
    e1 = dm.status(db_path)["expires_at"]
    time.sleep(0.01)
    dm.enable(db_path, ttl_seconds=200)
    e2 = dm.status(db_path)["expires_at"]
    assert int(e2) >= int(e1)


def test_is_debug_active_env_alone_does_not_activate(db_path, monkeypatch):
    """0.8.8: 環境変数 PERSONA_MEMORY_DEBUG では debug mode は起動しない.

    回帰防止: env 経路 (= 旧経路) を復活させないこと. flag 経路 (set_debug_mode)
    のみが debug mode の起動経路という設計の単一化を担保する.
    """
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "1")
    # flag 不在 → env が立っていても inactive
    assert dm.is_debug_active(db_path) is False
    assert dm.is_debug_active(None) is False


def test_is_debug_active_via_flag(db_path, monkeypatch):
    monkeypatch.delenv("PERSONA_MEMORY_DEBUG", raising=False)
    dm.enable(db_path)
    assert dm.is_debug_active(db_path) is True


def test_is_debug_active_flag_wins_over_env(db_path, monkeypatch):
    """env が立っていても flag が無効なら inactive (回帰防止)."""
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "1")
    # flag を一度 enable → disable で消す
    dm.enable(db_path)
    dm.disable(db_path)
    assert dm.is_debug_active(db_path) is False


def test_status_with_remaining_seconds(db_path):
    dm.enable(db_path, ttl_seconds=600, reason="調査")
    s = dm.status(db_path)
    assert s["active"] is True
    assert s["reason"] == "調査"
    assert int(s["remaining_seconds"]) > 0
    assert int(s["remaining_seconds"]) <= 600
