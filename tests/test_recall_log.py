"""recall debug log (scripts.debug.recall_log) のテスト (0.8.9).

0.8.9 rename: 環境変数 PERSONA_MEMORY_DEBUG (= 同名で debug mode の責務 A を
0.8.8 まで持っていた) を PERSONA_RECALL_LOG_LEVEL に rename して名前空間を
クリーンにする. 旧名は読まれない (= 後方互換無し, design drift 回避).
"""
from __future__ import annotations

import pytest


def _fresh_resolve(monkeypatch):
    """env を反映した状態で _resolve_level を呼ぶヘルパ.

    関数内 import で monkeypatch の効いた env を確実に反映させる.
    """
    from scripts.debug.recall_log import _resolve_level, is_enabled
    return _resolve_level(), is_enabled()


def test_recall_log_disabled_by_default(monkeypatch):
    """env 未設定なら OFF (level 0, is_enabled False)."""
    monkeypatch.delenv("PERSONA_RECALL_LOG_LEVEL", raising=False)
    monkeypatch.delenv("PERSONA_MEMORY_DEBUG", raising=False)
    level, enabled = _fresh_resolve(monkeypatch)
    assert level == 0
    assert enabled is False


@pytest.mark.parametrize("env_val,expected_level", [
    ("a", 1),
    ("b", 2),
    ("c", 3),
    ("A", 1),  # 大文字も OK
    ("1", 3),  # default = 最詳細
    ("true", 3),
    ("on", 3),
    ("0", 0),
    ("", 0),
    ("false", 0),
    ("off", 0),
    ("xyz", 3),  # 未知値 → 安全側 default (= 最詳細)
])
def test_recall_log_level_resolution(monkeypatch, env_val, expected_level):
    """PERSONA_RECALL_LOG_LEVEL の値ごとの level マッピング."""
    monkeypatch.delenv("PERSONA_MEMORY_DEBUG", raising=False)
    monkeypatch.setenv("PERSONA_RECALL_LOG_LEVEL", env_val)
    level, _ = _fresh_resolve(monkeypatch)
    assert level == expected_level, f"{env_val!r} → {level} (期待 {expected_level})"


def test_recall_log_old_env_name_ignored(monkeypatch):
    """0.8.9 rename 回帰: 旧名 PERSONA_MEMORY_DEBUG は読まれない.

    後方互換無しの設計判断 (= design_drift_zero_tolerance). 旧名を立てても
    完全 OFF 扱い. ユーザー側で env を新名に更新する必要があるが、 立て忘れ
    でも「ログが出ない」 だけで本処理は無傷.
    """
    monkeypatch.delenv("PERSONA_RECALL_LOG_LEVEL", raising=False)
    monkeypatch.setenv("PERSONA_MEMORY_DEBUG", "c")
    level, enabled = _fresh_resolve(monkeypatch)
    assert level == 0
    assert enabled is False


def test_recall_log_new_env_takes_effect(monkeypatch):
    """新名 PERSONA_RECALL_LOG_LEVEL=c で最詳細 ON になる正規経路."""
    monkeypatch.delenv("PERSONA_MEMORY_DEBUG", raising=False)
    monkeypatch.setenv("PERSONA_RECALL_LOG_LEVEL", "c")
    level, enabled = _fresh_resolve(monkeypatch)
    assert level == 3
    assert enabled is True
