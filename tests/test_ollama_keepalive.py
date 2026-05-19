"""Ollama keep_alive / prewarm の仕様テスト (0.8.6 改修).

ご主人様確定済仕様:
- keep_alive は 3 分 (最後のアクセスから 3 分でモデル解放)
- アクセスのたびにタイマーは Ollama 側で更新 (= keep_alive parameter の挙動)
- SessionStart での prewarm は廃止 (= モデル常駐させない)
"""
from __future__ import annotations

import importlib


def test_default_keep_alive_is_3_minutes():
    """OllamaClient のデフォルト keep_alive は 3 分."""
    import scripts.shared.ollama as ollama_mod
    importlib.reload(ollama_mod)
    assert ollama_mod.DEFAULT_KEEP_ALIVE == "3m"


def test_prewarm_module_is_removed():
    """0.8.6 で prewarm 機構は廃止. モジュール自体が存在しない."""
    import importlib.util
    spec = importlib.util.find_spec("scripts.shared.prewarm")
    assert spec is None, "scripts.shared.prewarm は廃止されたはず"


def test_spawn_prewarm_is_removed():
    """spawn_prewarm ヘルパも spawn.py から削除されている."""
    from scripts.hooks import spawn as spawn_mod
    assert not hasattr(spawn_mod, "spawn_prewarm")


def test_on_session_start_does_not_call_prewarm():
    """SessionStart hook 内で prewarm 起動の経路がない."""
    import scripts.hooks.on_session_start as oss
    source = open(oss.__file__).read()
    assert "spawn_prewarm" not in source
    assert "prewarm" not in source.lower()
