"""セッション内話題シフト検知.

設計の核:
- user_prompt 受信時に「直前 N 発話 vs 新規 prompt」 を light LLM が比較
- 「話題転換」 と判定したら新 topic_id を発行 + active-topic を切替
- 「継続」 ならそのまま (= 既定動作)

これで 1 セッション内に複数の topic_id が並ぶことが可能になり、 recall 候補
絞り込みが粗くなる問題 (Phase 3.1 残課題) を構造的に解消する.

PERSONA_TOPIC_SHIFT_DISABLE=1 で完全 bypass.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass

from pycozo.client import Client

from scripts.db_cozo.repo import (
    ensure_topic, fetch_recent_episodes_for_topic,
    get_active_topic, set_active_topic,
)
from scripts.shared.ollama import LLMClient

SHIFT_MODEL = os.environ.get(
    "PERSONA_TOPIC_SHIFT_MODEL",
    os.environ.get("PERSONA_LIGHT_MODEL", "gemma3:4b"),
)
SHIFT_NUM_CTX = int(os.environ.get("PERSONA_TOPIC_SHIFT_NUM_CTX", "4096"))
# シフト検知を始める下限. 新 topic 作成直後は誤判定しやすいので待機.
SHIFT_MIN_EPISODES = int(os.environ.get("PERSONA_TOPIC_SHIFT_MIN_EPISODES", "3"))


_PROMPT_TEMPLATE = """\
以下は同一の議論トピックに紐付いている直近の発話ログです.
ユーザーが今回投げた **新規発話** が、 直近のトピックの「継続」 か
「別トピック」 への切り替えかを判定してください.

**判定基準 (厳しめ. 迷ったら継続):**
- 「continuation」: 直近の主題 / 機能 / 議論対象を続けている.
  サブ論点 / 命名 / 詳細詰めも継続扱い (= shift しない).
- 「shift」: 主題語が明確に別物に変わった.
  例: 「ところで」「別件で」「話戻すけど」 で始まる, 全く違う機能名・固有名詞.

直近発話 (新→古):
{recent}

新規発話 (user):
{new}

**判定 (continuation / shift どちらか) を answer に入れる**.
JSON のみ (説明・前置き・コードフェンス禁止):
{{"answer": "continuation" | "shift", "reason": "1 行の理由"}}
"""


@dataclass
class ShiftJudgment:
    shift: bool
    reason: str


def build_prompt(recent_eps: list[dict], new_prompt: str) -> str:
    if recent_eps:
        recent = "\n".join(
            f"[{e.get('role','?')}] {(e.get('content') or '').strip()[:200]}"
            for e in recent_eps
        )
    else:
        recent = "(なし)"
    return _PROMPT_TEMPLATE.format(recent=recent, new=new_prompt[:500])


def parse_judgment(text: str) -> ShiftJudgment:
    if not text:
        return ShiftJudgment(False, "(empty response)")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return ShiftJudgment(False, "(no JSON)")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return ShiftJudgment(False, "(invalid JSON)")
    if not isinstance(data, dict):
        return ShiftJudgment(False, "(not dict)")
    # 新フォーマット: answer = "continuation" | "shift"
    answer = data.get("answer")
    if isinstance(answer, str):
        is_shift = answer.strip().lower() == "shift"
    else:
        # 旧フォーマット fallback (shift bool)
        is_shift = bool(data.get("shift", False))
    return ShiftJudgment(
        shift=is_shift,
        reason=str(data.get("reason") or ""),
    )


def detect_shift(
    recent_eps: list[dict], new_prompt: str,
    client: LLMClient, model: str = SHIFT_MODEL,
) -> ShiftJudgment:
    """直前 episodes と新発話を LLM が比較. 失敗時は False (= 安全側)."""
    if not new_prompt.strip():
        return ShiftJudgment(False, "(empty prompt)")
    prompt = build_prompt(recent_eps, new_prompt)
    try:
        response = client.generate(model, prompt, num_ctx=SHIFT_NUM_CTX)
    except Exception as e:
        return ShiftJudgment(False, f"(LLM failed: {e})")
    return parse_judgment(response)


def topic_shift_disabled() -> bool:
    return os.environ.get("PERSONA_TOPIC_SHIFT_DISABLE", "").strip() == "1"


def maybe_split_topic(
    cozo: Client, session_id: str, new_prompt: str, llm: LLMClient,
) -> tuple[str, ShiftJudgment | None]:
    """user_prompt 受信時に呼ぶ. 必要なら新 topic_id へ切替.

    戻り値: (active topic_id (切替後), 判定結果 or None).
    判定 None = bypass / 不要だった.
    """
    if topic_shift_disabled():
        return get_active_topic(cozo, session_id), None
    current = get_active_topic(cozo, session_id)
    recent = fetch_recent_episodes_for_topic(cozo, current, last_n=SHIFT_MIN_EPISODES)
    if len(recent) < SHIFT_MIN_EPISODES:
        # まだ蓄積が薄い → 判定しない (誤判定を避ける)
        return current, None
    judgment = detect_shift(recent, new_prompt, llm)
    if not judgment.shift:
        return current, judgment
    # 話題転換 → 新 topic_id を発行
    new_topic_id = f"sub-{uuid.uuid4().hex[:12]}"
    ensure_topic(cozo, new_topic_id)
    set_active_topic(cozo, session_id, new_topic_id)
    return new_topic_id, judgment
