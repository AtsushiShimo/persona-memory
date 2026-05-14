"""scripts.db_cozo.topic_shift の Cross-Session Topic Merge ロジックのテスト.

既存 topic_shift テスト (test_db_cozo_topic_shift.py) は「shift 判定」 のみを
覆っているので, この追加テストは「shift = true 時に past topic へ merge できるか」
の境界を確認する.

通すためのテストではなく, 設計の境界 (= どの条件で merge が発火するか)
を露出させるのが目的.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.repo import (
    ensure_topic, get_active_topic, save_episode, set_active_topic,
)
from scripts.db_cozo.topic_shift import (
    MergeJudgment, build_merge_prompt, find_past_topic_for_merge,
    maybe_split_topic, parse_merge_judgment,
)


# ── テストヘルパ ─────────────────────────────────────────────────────────

VEC_DIM = 768


def _vec(seed: float) -> list[float]:
    """テスト用の擬似 embedding. seed で内容差を作る."""
    return [seed] + [0.0] * (VEC_DIM - 1)


def _put_episode_with_emb(
    client, episode_id: int, role: str, content: str,
    session_id: str, topic_id: str, embedding: list[float],
) -> None:
    """テスト用. episode を直接 put して embedding まで埋める."""
    ts = "2026-05-14T00:00:00"
    client.run(
        "?[id, role, content, session_id, topic_id, timestamp, embedding] <- "
        "[[$id, $role, $content, $sid, $tid, $ts, $emb]] "
        ":put episode {id => role, content, session_id, topic_id, timestamp, embedding}",
        {"id": episode_id, "role": role, "content": content, "sid": session_id,
         "tid": topic_id, "ts": ts, "emb": embedding},
    )


@dataclass
class FakeMergeClient:
    """generate / embed の両方を切り替え可能な fake LLM client."""
    generate_responses: list[str] = field(default_factory=list)
    embed_vec: list[float] = field(default_factory=lambda: _vec(0.99))
    generate_calls: list[str] = field(default_factory=list)

    def generate(self, model, prompt, num_ctx=None):
        self.generate_calls.append(prompt)
        if not self.generate_responses:
            return '{"answer": "different", "reason": "default"}'
        return self.generate_responses.pop(0)

    def embed(self, model, text):
        return self.embed_vec


@pytest.fixture
def client(tmp_path: Path):
    return init_db(tmp_path / "p.cozo.db")


# ── parse_merge_judgment ─────────────────────────────────────────────────

def test_parse_merge_judgment_match():
    is_match, reason = parse_merge_judgment(
        '{"answer": "match", "reason": "same topic"}'
    )
    assert is_match is True
    assert reason == "same topic"


def test_parse_merge_judgment_different():
    is_match, _ = parse_merge_judgment(
        '{"answer": "different", "reason": "x"}'
    )
    assert is_match is False


def test_parse_merge_judgment_invalid_returns_false():
    assert parse_merge_judgment("").match if False else parse_merge_judgment("")[0] is False
    assert parse_merge_judgment("not json")[0] is False


def test_parse_merge_judgment_code_fence():
    is_match, _ = parse_merge_judgment(
        '```json\n{"answer": "match", "reason": "x"}\n```'
    )
    assert is_match is True


def test_build_merge_prompt_includes_past_and_new():
    p = build_merge_prompt(
        [{"role": "user", "content": "Renju 開発の続き"}],
        "Renju のサイドバー設計",
    )
    assert "Renju" in p
    assert "サイドバー" in p


# ── find_past_topic_for_merge ────────────────────────────────────────────

def test_find_past_topic_returns_none_when_no_candidates(client):
    """DB に episodes が無ければ何も返らない."""
    fake = FakeMergeClient()
    out = find_past_topic_for_merge(client, "新規発話", None, fake)
    assert out.match is False
    assert out.topic_id is None


def test_find_past_topic_returns_topic_when_llm_says_match(client):
    """vec 近傍が見つかり、 LLM が match と判定すれば topic_id を返す."""
    ensure_topic(client, "t-renju")
    target_vec = _vec(0.5)
    # 同じ embedding を持つ episodes を Renju topic に積む
    _put_episode_with_emb(
        client, 1, "user", "Renju のサイドバー設計を詰めよう " + "a" * 50,
        "s-past", "t-renju", target_vec,
    )
    _put_episode_with_emb(
        client, 2, "assistant", "了解. カテゴリどうする? " + "b" * 50,
        "s-past", "t-renju", target_vec,
    )

    fake = FakeMergeClient(
        generate_responses=['{"answer": "match", "reason": "Renju の続き"}'],
        embed_vec=target_vec,  # 同じ vec で query → 必ず hit
    )
    out = find_past_topic_for_merge(client, "Renju の続き", None, fake)
    assert out.match is True
    assert out.topic_id == "t-renju"


def test_find_past_topic_returns_no_match_when_llm_says_different(client):
    """vec 近傍は hit するが LLM が different と返せば match=False."""
    ensure_topic(client, "t-coffee")
    target_vec = _vec(0.3)
    _put_episode_with_emb(
        client, 10, "user", "コーヒー深煎り好き " + "a" * 50,
        "s-old", "t-coffee", target_vec,
    )
    _put_episode_with_emb(
        client, 11, "user", "砂糖は要らない " + "b" * 50,
        "s-old", "t-coffee", target_vec,
    )

    fake = FakeMergeClient(
        generate_responses=['{"answer": "different", "reason": "別議題"}'],
        embed_vec=target_vec,
    )
    out = find_past_topic_for_merge(client, "全然違う話", None, fake)
    assert out.match is False
    assert out.topic_id is None


def test_find_past_topic_excludes_current_topic(client):
    """exclude_topic_id で渡された現 topic は候補から除外."""
    ensure_topic(client, "t-current")
    target_vec = _vec(0.7)
    _put_episode_with_emb(
        client, 20, "user", "現在の話題 " + "a" * 50,
        "s1", "t-current", target_vec,
    )
    fake = FakeMergeClient(embed_vec=target_vec)
    out = find_past_topic_for_merge(client, "現在の話題に近い", "t-current", fake)
    # 除外された結果, 候補ゼロで終わる
    assert out.match is False
    assert out.topic_id is None
    # LLM 判定までは到達していない (= 候補なしで早期 return)
    assert len(fake.generate_calls) == 0


def test_find_past_topic_disabled_via_env(client, monkeypatch):
    monkeypatch.setenv("PERSONA_TOPIC_MERGE_DISABLE", "1")
    ensure_topic(client, "t-x")
    target_vec = _vec(0.4)
    _put_episode_with_emb(
        client, 30, "user", "x " + "a" * 50, "s1", "t-x", target_vec,
    )
    fake = FakeMergeClient(embed_vec=target_vec)
    out = find_past_topic_for_merge(client, "y", None, fake)
    assert out.match is False
    # disabled なので embed も generate も呼ばれない
    assert len(fake.generate_calls) == 0


def test_find_past_topic_handles_empty_embedding(client):
    """embed が空リストを返す場合は match なしで終わる (LLM 呼ばない)."""
    ensure_topic(client, "t-y")
    target_vec = _vec(0.6)
    _put_episode_with_emb(
        client, 40, "user", "y " + "a" * 50, "s1", "t-y", target_vec,
    )
    fake = FakeMergeClient(embed_vec=[])  # 空 → 短絡
    out = find_past_topic_for_merge(client, "test", None, fake)
    assert out.match is False
    assert len(fake.generate_calls) == 0


# ── maybe_split_topic の merge 経路 ──────────────────────────────────────

def _seed_renju_topic(client) -> str:
    """テストヘルパ: Renju 議論を t-renju に積む."""
    ensure_topic(client, "t-renju")
    target_vec = _vec(0.5)
    _put_episode_with_emb(
        client, 1, "user", "Renju の議論開始 " + "a" * 50,
        "s-old", "t-renju", target_vec,
    )
    _put_episode_with_emb(
        client, 2, "assistant", "サイドバー設計検討 " + "b" * 50,
        "s-old", "t-renju", target_vec,
    )
    _put_episode_with_emb(
        client, 3, "user", "次論点はレイアウト " + "c" * 50,
        "s-old", "t-renju", target_vec,
    )
    return target_vec


def test_maybe_split_topic_merges_to_past_when_few_episodes_match(client):
    """新セッション初回 (蓄積不足) で past topic に match すれば merge.

    シナリオ A の Session 2 冒頭. 新セッション開始直後で recent < SHIFT_MIN
    だが、 Renju topic に過去の議論が積まれている → merge 経路で復帰.
    """
    target_vec = _seed_renju_topic(client)
    # 新セッション s-new を開始 (まだ蓄積ゼロ)
    set_active_topic(client, "s-new", "s-new")  # 既定 = session_id 自体
    fake = FakeMergeClient(
        generate_responses=['{"answer": "match", "reason": "Renju 続き"}'],
        embed_vec=target_vec,
    )
    out, judgment = maybe_split_topic(
        client, "s-new", "Renju の話の続き", fake,
    )
    assert out == "t-renju"
    assert get_active_topic(client, "s-new") == "t-renju"
    # 蓄積不足経路なので shift judgment は None
    assert judgment is None


def test_maybe_split_topic_keeps_when_few_episodes_and_no_merge(client):
    """蓄積不足 + 過去 topic にも match しなければ現状維持."""
    set_active_topic(client, "s-new", "s-new")
    fake = FakeMergeClient(embed_vec=_vec(0.99))  # 過去エピソード無し
    out, judgment = maybe_split_topic(
        client, "s-new", "Hello", fake,
    )
    assert out == "s-new"  # 現 topic_id 維持
    assert judgment is None


def test_maybe_split_topic_merges_when_shift_detected_with_match(client):
    """セッション内で shift = true → past topic merge match で過去 topic に切替.

    シナリオ B の混入後復帰. 現セッションで雑談 → 「Renju に戻ろう」 と
    投げた時, shift = true で merge 判定が走り、 過去 Renju に戻る.
    """
    target_vec = _seed_renju_topic(client)
    # 現セッション s-cur は雑談中 (別 topic) として 4 episode 積む.
    # vec_idx は null embedding を許容しないので, ダミー vec を持たせる.
    ensure_topic(client, "t-cur")
    set_active_topic(client, "s-cur", "t-cur")
    chitchat_vec = _vec(0.95)
    for i in range(4):
        _put_episode_with_emb(
            client, 100 + i, "user", f"雑談 {i} " + "x" * 80,
            "s-cur", "t-cur", chitchat_vec,
        )

    fake = FakeMergeClient(
        generate_responses=[
            '{"answer": "shift", "reason": "別話題"}',     # detect_shift
            '{"answer": "match", "reason": "Renju 復帰"}',  # merge judge
        ],
        embed_vec=target_vec,
    )
    out, judgment = maybe_split_topic(
        client, "s-cur", "Renju に戻ろう", fake,
    )
    assert out == "t-renju"
    assert judgment is not None and judgment.shift is True


def test_maybe_split_topic_creates_new_topic_when_shift_and_no_merge(client):
    """shift = true & merge = no match で新 topic_id を発行 (既存 split 経路)."""
    ensure_topic(client, "t-cur")
    set_active_topic(client, "s-cur", "t-cur")
    chitchat_vec = _vec(0.95)
    for i in range(4):
        _put_episode_with_emb(
            client, 200 + i, "user", f"a {i} " + "x" * 80,
            "s-cur", "t-cur", chitchat_vec,
        )
    fake = FakeMergeClient(
        generate_responses=[
            '{"answer": "shift", "reason": "別話題"}',
            # merge 判定では候補 hit するが LLM が different と返す
            '{"answer": "different", "reason": "違う"}',
        ],
        embed_vec=_vec(0.99),  # chitchat と離れた vec
    )
    out, judgment = maybe_split_topic(
        client, "s-cur", "全然違う話", fake,
    )
    assert out != "t-cur"
    assert out.startswith("sub-")
    assert judgment is not None and judgment.shift is True
