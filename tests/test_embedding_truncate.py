"""embedding 入力テキストの truncate ロジックのテスト.

nomic-embed-text のコンテキスト窓 (~2048 token) を超えると Ollama が 500
を返す事故 (カサンドラ DB の 7 件 episode 失敗) を防ぐため、 0.5.28 で
embed 投入前の truncate を導入した. 全 caller が一括で恩恵を受けるよう
OllamaClient.embed() の中で適用する.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from scripts.shared.embedding import (
    EMBED_TEXT_MAX_CHARS,
    truncate_for_embedding,
)


def test_truncate_passes_short_text_through():
    assert truncate_for_embedding("hello") == "hello"


def test_truncate_passes_exact_max_length_through():
    s = "あ" * EMBED_TEXT_MAX_CHARS
    assert truncate_for_embedding(s) == s


def test_truncate_cuts_overlong_text():
    s = "あ" * (EMBED_TEXT_MAX_CHARS + 100)
    out = truncate_for_embedding(s)
    assert len(out) == EMBED_TEXT_MAX_CHARS
    assert out == s[:EMBED_TEXT_MAX_CHARS]


def test_truncate_handles_empty_and_none():
    assert truncate_for_embedding("") == ""
    assert truncate_for_embedding(None) == ""  # type: ignore[arg-type]


def test_truncate_respects_custom_max():
    assert truncate_for_embedding("abcdef", max_chars=3) == "abc"


def test_ollama_client_embed_truncates_before_post():
    """OllamaClient.embed が長文を受けても POST 時点で 3000 文字以内になる."""
    from scripts.shared.ollama import OllamaClient

    long_text = "あ" * 5000
    with patch("scripts.shared.ollama.httpx.post") as mp:
        mp.return_value = MagicMock(
            raise_for_status=lambda: None,
            json=lambda: {"embedding": [0.1] * 768},
        )
        OllamaClient().embed("nomic-embed-text", long_text)

    # POST に渡された prompt が truncate されている
    kwargs = mp.call_args.kwargs
    sent_prompt = kwargs["json"]["prompt"]
    assert len(sent_prompt) == EMBED_TEXT_MAX_CHARS


def test_ollama_client_embed_empty_returns_empty_without_post():
    """空文字を渡しても POST せず空 list を返す (Ollama 500 を未然に防ぐ)."""
    from scripts.shared.ollama import OllamaClient

    with patch("scripts.shared.ollama.httpx.post") as mp:
        result = OllamaClient().embed("nomic-embed-text", "")
    assert result == []
    mp.assert_not_called()
