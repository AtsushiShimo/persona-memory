"""怒気検知 (scripts.reflection.detect) のテスト."""
from __future__ import annotations

import os

import pytest

from scripts.reflection.detect import detect_anger


class _FakeLLMAnger:
    """常に anger を返す LLM (2 段目). detect_anger 結果は True 固定."""

    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        return '{"answer": "anger", "reason": "ok"}'

    def embed(self, model: str, text: str) -> list[float]:
        return []


class _FakeLLMNeutral:
    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        return '{"answer": "neutral", "reason": "ok"}'

    def embed(self, model: str, text: str) -> list[float]:
        return []


class _FakeLLMFail:
    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        raise RuntimeError("LLM unavailable")

    def embed(self, model: str, text: str) -> list[float]:
        return []


def test_disabled_returns_false(monkeypatch):
    monkeypatch.setenv("PERSONA_ANGER_DETECT_DISABLE", "1")
    angry, phrase = detect_anger("ちげーよ", _FakeLLMAnger())
    assert angry is False


def test_no_keyword_returns_false():
    angry, phrase = detect_anger("コードレビューお願いします", _FakeLLMAnger())
    assert angry is False
    assert phrase == ""


@pytest.mark.parametrize("text", [
    "ちげーよ",
    "違うって言ってるだろ",
    "やめろよ",
    "なんで分からないんだよ",
    "視野が狭過ぎる",
    "クソみたいな実装",
    "致命的だな",
    "意味が分からない",
    "勝手に分けるな",
])
def test_keyword_matches(text):
    angry, phrase = detect_anger(text, _FakeLLMAnger())
    assert angry is True
    assert phrase != ""


def test_llm_disabled_returns_true_on_keyword(monkeypatch):
    """LLM 2 段目を off にしたら keyword だけで発火 (= 更に強め)."""
    monkeypatch.setenv("PERSONA_ANGER_LLM_DISABLE", "1")
    angry, phrase = detect_anger("ちげーよ", _FakeLLMAnger())
    assert angry is True


def test_llm_neutral_suppresses_false_positive():
    """keyword ヒットしても LLM が neutral 返したら抑制."""
    # "違う" を含むが文脈は中立 (例: 別件の話題転換)
    angry, _ = detect_anger("話が違うんですが、 こちらの件で", _FakeLLMNeutral())
    assert angry is False


def test_llm_failure_falls_back_to_anger():
    """LLM 失敗時は安全側 = anger と判定 (= 取りこぼし禁止)."""
    angry, _ = detect_anger("ちげーよ、 やり直して", _FakeLLMFail())
    assert angry is True


def test_empty_content_returns_false():
    angry, _ = detect_anger("", _FakeLLMAnger())
    assert angry is False


def test_none_llm_with_keyword_returns_true():
    """LLM None 渡し時は keyword だけで判定 (低レイテンシ環境用)."""
    angry, phrase = detect_anger("やめろ", None)
    assert angry is True
    assert phrase == "やめろ"
