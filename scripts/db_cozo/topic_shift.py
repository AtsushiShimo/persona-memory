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
    find_similar_topics_by_emb,
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
# Cross-Session Topic Merge 設定
MERGE_MODEL = os.environ.get(
    "PERSONA_TOPIC_MERGE_MODEL",
    os.environ.get("PERSONA_LIGHT_MODEL", "gemma3:4b"),
)
MERGE_NUM_CTX = int(os.environ.get("PERSONA_TOPIC_MERGE_NUM_CTX", "4096"))
MERGE_CANDIDATE_K = int(os.environ.get("PERSONA_TOPIC_MERGE_TOP_K", "5"))
MERGE_DISTANCE_MAX = float(os.environ.get("PERSONA_TOPIC_MERGE_DISTANCE_MAX", "0.6"))
EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")


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


def topic_merge_disabled() -> bool:
    return os.environ.get("PERSONA_TOPIC_MERGE_DISABLE", "").strip() == "1"


_MERGE_PROMPT_TEMPLATE = """\
以下は同じ議論トピックの可能性がある過去の発話群です.
新規発話と過去 topic の代表発話を比較し、 これらが同じ議題
(= 主題 / 機能 / 議論対象が同じ) かを判定してください.

**判定基準 (厳しめ. 迷ったら different):**
- "match": 主題語 / 機能名 / 議論対象が一致. 議論の続きとして自然.
- "different": 主題が異なる. 単語の偶然一致は different.

過去 topic の代表発話 (新→古):
{past}

新規発話:
{new}

JSON のみ (説明・前置き・コードフェンス禁止):
{{"answer": "match" | "different", "reason": "1 行"}}
"""


@dataclass
class MergeJudgment:
    match: bool
    topic_id: str | None
    reason: str


def build_merge_prompt(past_eps: list[dict], new_prompt: str) -> str:
    if past_eps:
        past = "\n".join(
            f"[{e.get('role','?')}] {(e.get('content') or '').strip()[:200]}"
            for e in past_eps
        )
    else:
        past = "(なし)"
    return _MERGE_PROMPT_TEMPLATE.format(past=past, new=new_prompt[:500])


def parse_merge_judgment(text: str) -> tuple[bool, str]:
    """戻り値: (is_match, reason)"""
    if not text:
        return False, "(empty response)"
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return False, "(no JSON)"
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False, "(invalid JSON)"
    if not isinstance(data, dict):
        return False, "(not dict)"
    answer = data.get("answer")
    is_match = isinstance(answer, str) and answer.strip().lower() == "match"
    return is_match, str(data.get("reason") or "")


def find_past_topic_for_merge(
    cozo: Client, new_prompt: str, exclude_topic_id: str | None,
    llm: LLMClient, embed_model: str = EMBED_MODEL,
    merge_model: str = MERGE_MODEL,
) -> MergeJudgment:
    """新発話 prompt に対し、 過去 topic で merge 可能なものを探す.

    手順:
    1. new_prompt を embed
    2. find_similar_topics_by_emb で候補取得 (現 topic 除外)
    3. 各候補について fetch_recent_episodes_for_topic で代表発話取得
    4. light LLM が「同議題か?」 判定. 最初に match と返した candidate を採用
    """
    if topic_merge_disabled():
        return MergeJudgment(False, None, "(merge disabled)")
    if not new_prompt.strip():
        return MergeJudgment(False, None, "(empty prompt)")
    try:
        emb = llm.embed(embed_model, new_prompt)
    except Exception as e:
        return MergeJudgment(False, None, f"(embed failed: {e})")
    if not emb:
        return MergeJudgment(False, None, "(empty embedding)")
    candidates = find_similar_topics_by_emb(
        cozo, emb, top_k=MERGE_CANDIDATE_K,
        exclude_topic_id=exclude_topic_id,
        distance_max=MERGE_DISTANCE_MAX,
    )
    if not candidates:
        return MergeJudgment(False, None, "(no candidates)")
    for cand in candidates:
        tid = cand["topic_id"]
        past_eps = fetch_recent_episodes_for_topic(cozo, tid, last_n=5)
        if not past_eps:
            continue
        prompt = build_merge_prompt(past_eps, new_prompt)
        try:
            response = llm.generate(merge_model, prompt, num_ctx=MERGE_NUM_CTX)
        except Exception:
            continue
        is_match, reason = parse_merge_judgment(response)
        if is_match:
            return MergeJudgment(True, tid, reason or "match")
    return MergeJudgment(False, None, "(no match in candidates)")


def maybe_split_topic(
    cozo: Client, session_id: str, new_prompt: str, llm: LLMClient,
) -> tuple[str, ShiftJudgment | None]:
    """user_prompt 受信時に呼ぶ. 必要なら topic_id 切替 (merge or split).

    判定フロー:
    1. shift_min 未満なら何もしない
    2. shift = false → 継続
    3. shift = true → past topic への merge 判定:
       - match → 既存 topic_id に切替 (Cross-Session Topic Merge)
       - no match → 新 topic_id を発行 (現行 split)

    戻り値: (active topic_id (切替後), shift 判定結果 or None).
    判定 None = bypass / 不要だった.
    """
    if topic_shift_disabled():
        return get_active_topic(cozo, session_id), None
    current = get_active_topic(cozo, session_id)
    recent = fetch_recent_episodes_for_topic(cozo, current, last_n=SHIFT_MIN_EPISODES)
    if len(recent) < SHIFT_MIN_EPISODES:
        # まだ蓄積が薄い → split 判定しない. ただし新セッション初回として
        # past topic への merge は試みる (session 跨ぎ復帰のケース).
        merge = find_past_topic_for_merge(cozo, new_prompt, current, llm)
        if merge.match and merge.topic_id:
            ensure_topic(cozo, merge.topic_id)
            set_active_topic(cozo, session_id, merge.topic_id)
            return merge.topic_id, None
        return current, None
    judgment = detect_shift(recent, new_prompt, llm)
    if not judgment.shift:
        return current, judgment
    # 話題転換検知 → まず past topic merge を試す
    merge = find_past_topic_for_merge(cozo, new_prompt, current, llm)
    if merge.match and merge.topic_id:
        ensure_topic(cozo, merge.topic_id)
        set_active_topic(cozo, session_id, merge.topic_id)
        return merge.topic_id, judgment
    # merge 不成立 → 新 topic_id を発行 (現行 split)
    new_topic_id = f"sub-{uuid.uuid4().hex[:12]}"
    ensure_topic(cozo, new_topic_id)
    set_active_topic(cozo, session_id, new_topic_id)
    return new_topic_id, judgment
