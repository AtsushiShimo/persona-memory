"""search_memory MCP tool のデフォルト引数のテスト.

0.5.29: include_episodes のデフォルトを False → True に変更.
理由: main agent が「過去の議論内容を辿る」 のが自発 recall の主用途で、
default で episodes も拾えないと「自覚」 で呼んでも生ログに辿れず不便.
"""
from __future__ import annotations

import inspect


def test_search_memory_default_includes_episodes_is_true():
    """search_memory の include_episodes パラメータの default が True."""
    from server.main import search_memory

    # FastMCP の tool object は内部で .fn / .func に元の関数を保持している
    fn = (
        getattr(search_memory, "fn", None)
        or getattr(search_memory, "func", None)
        or search_memory
    )
    sig = inspect.signature(fn)
    assert "include_episodes" in sig.parameters
    assert sig.parameters["include_episodes"].default is True, (
        "0.5.29 で include_episodes default は True に変更されたはず"
    )
