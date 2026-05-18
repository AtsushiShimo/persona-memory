"""怒気検知 (scripts.reflection.detect) のテスト — 0.8.5 LLM-only 版."""
from __future__ import annotations

import pytest

from scripts.reflection.detect import detect_anger


class _FakeLLMAnger:
    """常に anger を返す LLM. detect_anger 結果は True 固定."""

    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        return '{"answer": "anger", "phrase": "ちげー"}'

    def embed(self, model: str, text: str) -> list[float]:
        return []


class _FakeLLMAngerNoPhrase:
    """anger だが phrase 抽出を返さない LLM. fallback で content[:40] が入る."""

    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        return '{"answer": "anger"}'

    def embed(self, model: str, text: str) -> list[float]:
        return []


class _FakeLLMNeutral:
    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        return '{"answer": "neutral", "phrase": ""}'

    def embed(self, model: str, text: str) -> list[float]:
        return []


class _FakeLLMFail:
    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        raise RuntimeError("LLM unavailable")

    def embed(self, model: str, text: str) -> list[float]:
        return []


class _FakeLLMGarbage:
    """JSON 解析不能な出力を返す LLM. 安全側 (anger) に倒れる."""

    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        return "I cannot determine."

    def embed(self, model: str, text: str) -> list[float]:
        return []


def test_empty_content_returns_false():
    angry, _ = detect_anger("", _FakeLLMAnger())
    assert angry is False


def test_none_llm_returns_false():
    """LLM-only 仕様: llm 未渡し時は判定不能 → neutral 扱い."""
    angry, phrase = detect_anger("やめろ", None)
    assert angry is False
    assert phrase == ""


@pytest.mark.parametrize("text", [
    "ちげーよ",
    "違うって言ってるだろ",
    "やめろよ",
    "なんで分からないんだよ",
    "視野が狭過ぎる",
    "致命的だな",
    "勝手に分けるな",
    # 旧版 keyword フィルタを通らない弱い不満も拾えるのが LLM-only の眼目
    "ちょっとこれは想定と外れていますね",
    "もうちょっと考えてからやってほしいんだけど",
])
def test_llm_anger_fires(text):
    """LLM が anger と判定すれば、 keyword 一致の有無に関係なく発火."""
    angry, phrase = detect_anger(text, _FakeLLMAnger())
    assert angry is True
    assert phrase != ""


def test_llm_anger_without_phrase_falls_back_to_snippet():
    """LLM が phrase を省略しても発話冒頭 40 文字が入る."""
    angry, phrase = detect_anger("ちげーよやり直し", _FakeLLMAngerNoPhrase())
    assert angry is True
    assert phrase == "ちげーよやり直し"


def test_llm_neutral_suppresses():
    """LLM が neutral 返したら発火しない (= 通常依頼の取り違え防止)."""
    angry, _ = detect_anger("コードレビューお願いします", _FakeLLMNeutral())
    assert angry is False


def test_llm_neutral_on_ambiguous_phrase():
    """旧版だと keyword で誤発火していたケースも LLM 判定で抑制される."""
    angry, _ = detect_anger("話が違うんですが、 こちらの件で", _FakeLLMNeutral())
    assert angry is False


def test_llm_failure_falls_back_to_anger():
    """LLM 失敗時は安全側 = anger (= 取りこぼし禁止方針)."""
    angry, phrase = detect_anger("やり直して", _FakeLLMFail())
    assert angry is True
    assert phrase != ""  # snippet fallback


def test_llm_garbage_output_falls_back_to_anger():
    """JSON 解析不能でも安全側に倒す."""
    angry, phrase = detect_anger("これは違うと思う", _FakeLLMGarbage())
    assert angry is True
    assert phrase != ""


def test_phrase_truncated_to_40_chars():
    """phrase は最長 40 文字で切られる (instruction の長文化を防ぐ)."""
    angry, phrase = detect_anger("あ" * 200, _FakeLLMAngerNoPhrase())
    assert angry is True
    assert len(phrase) <= 40
