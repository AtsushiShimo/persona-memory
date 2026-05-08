"""recall summarize (関連性 curate + 自然文要約) のテスト."""
from __future__ import annotations

from dataclasses import dataclass

from scripts.recall.search import RecalledEpisode, RecalledFact
from scripts.recall.summarize import (
    EMPTY_MARKER,
    build_summarize_prompt,
    summarize_recall,
)


@dataclass
class FakeClient:
    response: str

    def generate(self, model, prompt):
        return self.response

    def embed(self, model, text):
        return []


def _fact(category="preference", key="coffee", value="深煎り", importance=6):
    return RecalledFact(
        fact_id=1, category=category, key=key, value=value,
        importance=importance, access_count=0, distance=0.1, score=0.5,
    )


def _episode():
    return RecalledEpisode(
        episode_id=1, role="user", content="コーヒーは深煎りが好き",
        timestamp="2026-05-07 14:00:00",
    )


def test_summarize_returns_empty_for_no_input():
    client = FakeClient(response="ignored")
    assert summarize_recall("質問", [], [], client) == ""


def test_summarize_returns_summary_text():
    client = FakeClient(response="マスターは深煎りコーヒーが好き。")
    out = summarize_recall("コーヒー何が好きだっけ", [_fact()], [], client)
    assert "深煎り" in out


def test_summarize_returns_empty_when_llm_says_no_match():
    """LLM が `(該当なし)` を返したら curate された空応答として扱う."""
    client = FakeClient(response=EMPTY_MARKER)
    out = summarize_recall("天気の話", [_fact()], [], client)
    assert out == ""


def test_summarize_strips_whitespace():
    client = FakeClient(response="   深煎りが好き  \n")
    out = summarize_recall("コーヒー", [_fact()], [], client)
    assert out == "深煎りが好き"


def test_summarize_returns_empty_on_llm_error():
    @dataclass
    class CrashClient:
        def generate(self, model, prompt):
            raise RuntimeError("oops")

        def embed(self, model, text):
            return []

    out = summarize_recall("コーヒー", [_fact()], [], CrashClient())
    assert out == ""


def test_build_summarize_prompt_includes_user_content_and_hits():
    prompt = build_summarize_prompt(
        "コーヒーは?", [_fact(value="深煎り好き")], [_episode()],
    )
    assert "コーヒーは?" in prompt
    assert "深煎り好き" in prompt
    assert "コーヒーは深煎りが好き" in prompt  # episode content
    # データ整形ツールとしての位置付けが含まれる
    assert "ツール" in prompt
    assert "対話・口調・人格は不要" in prompt
    # 関連性フィルタ指示が含まれる
    assert "関連" in prompt


def test_build_summarize_prompt_handles_empty_inputs():
    prompt = build_summarize_prompt("X", [], [])
    assert "(なし)" in prompt