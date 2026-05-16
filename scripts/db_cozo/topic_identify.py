"""「生きてる話題箱」 方式の topic 同定 (0.7.3).

設計の核 (2 段階):
- 1 段目 (embedding): 生きてる topic 群を summary embedding で並列照合し、
  ゆるめの閾値で上位 K 件の候補を絞る (= 候補プール)
- 2 段目 (LLM 追認): 候補プールの各 summary について light LLM が
  「新発話はこの話題の継続か?」 を YES/NO 判定. 最初に YES なら確定
- 候補ゼロ or 全 NO → 新 topic を作成

embedding だけだと意味的に近接した別話題 (例: 金融×プログラミング両方が
出てくる発話) で距離が拮抗するため誤判定が起きる. LLM の最終判定で
切り分ける. PERSONA_TOPIC_IDENTIFY_LLM_VERIFY_DISABLE=1 で 1 段目だけにも
できる (テスト・低レイテンシ環境用).

旧 topic_shift.py (active topic との 1 対 1 比較 + Cross-Session Merge) を
**完全に包摂** する設計.

env:
- PERSONA_TOPIC_IDENTIFY_DISABLE=1 で bypass (= 旧 get_active_topic にフォールバック)
- PERSONA_TOPIC_IDENTIFY_DISTANCE_MAX (default 0.45): 1 段目 (候補プール用) 閾値.
  この距離内の候補のみ LLM 確認に進む. ゆるめ.
- PERSONA_TOPIC_ALIVE_HOURS (default 168 = 1 週間): 「生きてる」 と見なす時間
- PERSONA_TOPIC_IDENTIFY_TOP_K (default 5): 候補件数
- PERSONA_TOPIC_IDENTIFY_LLM_VERIFY_DISABLE=1: LLM 追認を skip (embedding のみ).
  距離最近接が閾値内なら採用する旧モード.
- PERSONA_TOPIC_IDENTIFY_VERIFY_MODEL: 追認用 light モデル (default = gemma3:4b)
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass

from pycozo.client import Client

from scripts.db_cozo.repo import (
    add_topic_relation, ensure_topic, find_alive_topics_by_summary_emb,
    find_related_topics_by_summary_emb, get_active_topic,
    get_topic_summary_emb, set_active_topic, touch_topic,
    upsert_topic_summary_emb,
)
from scripts.db_cozo.topic_summary import (
    generate_initial_summary, update_summary,
)
from scripts.shared.ollama import LLMClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
DISTANCE_MAX = float(os.environ.get("PERSONA_TOPIC_IDENTIFY_DISTANCE_MAX", "0.45"))
ALIVE_HOURS = int(os.environ.get("PERSONA_TOPIC_ALIVE_HOURS", "168"))
TOP_K = int(os.environ.get("PERSONA_TOPIC_IDENTIFY_TOP_K", "5"))
VERIFY_MODEL = os.environ.get(
    "PERSONA_TOPIC_IDENTIFY_VERIFY_MODEL",
    os.environ.get("PERSONA_LIGHT_MODEL", "gemma3:4b"),
)
VERIFY_NUM_CTX = int(os.environ.get("PERSONA_TOPIC_IDENTIFY_VERIFY_NUM_CTX", "4096"))


def llm_verify_disabled() -> bool:
    return os.environ.get(
        "PERSONA_TOPIC_IDENTIFY_LLM_VERIFY_DISABLE", "",
    ).strip() == "1"


_VERIFY_PROMPT_TEMPLATE = """\
以下は今 active な話題の要約と、 新しい発話です. 新発話がこの話題の
「**継続**」 か「**別話題**」 かを判定してください.

判定基準 (厳しめ. 迷ったら 別話題):
- "match": 主題語 / 機能名 / 議論対象が一致. 議論の続きとして自然.
- "different": 主題が異なる. 単語の偶然一致 (例: 「Python」 と「データ」 が両方に
  含まれるだけ) は different.

話題の要約:
{summary}

新発話 ({role}):
{content}

JSON のみ (説明・前置き・コードフェンス禁止):
{{"answer": "match" | "different", "reason": "1 行"}}
"""


def _verify_topic_match(
    summary: str, role: str, content: str, llm: LLMClient,
    model: str = VERIFY_MODEL,
) -> bool:
    """LLM に「同じ話題か?」 を聞いて bool で返す. 失敗時は False (= 安全側)."""
    if not summary or not content.strip():
        return False
    prompt = _VERIFY_PROMPT_TEMPLATE.format(
        summary=summary[:600], role=role, content=content[:500],
    )
    try:
        response = llm.generate(model, prompt, num_ctx=VERIFY_NUM_CTX)
    except Exception:
        return False
    if not response:
        return False
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip(), flags=re.MULTILINE)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return False
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    answer = data.get("answer")
    return isinstance(answer, str) and answer.strip().lower() == "match"


def identify_disabled() -> bool:
    return os.environ.get("PERSONA_TOPIC_IDENTIFY_DISABLE", "").strip() == "1"


@dataclass
class IdentifyResult:
    topic_id: str
    is_new: bool
    matched_distance: float | None  # 既存 match した場合の距離. 新規時は None.
    summary: str  # 更新後 / 新規生成後の summary 本文


def identify_topic(
    client: Client,
    role: str,
    content: str,
    session_id: str,
    llm: LLMClient,
    embed_model: str = EMBED_MODEL,
    distance_max: float = DISTANCE_MAX,
    alive_hours: int = ALIVE_HOURS,
    top_k: int = TOP_K,
) -> IdentifyResult:
    """新発話を「生きてる話題箱」 群と照合し、 紐付け先 topic_id を返す.

    手順:
      1. 新発話 embedding 化
      2. 生きてる topic 群を summary embedding で並列照合
      3. 最近接が distance_max 内 → その topic に紐付け
         - summary を update_summary で更新 → upsert_topic_summary_emb
         - touch_topic で last_active_at 更新
         - set_active_topic で active topic にセット
      4. 該当なし → 新 topic_id を発行
         - generate_initial_summary で初期 summary 生成
         - upsert_topic_summary_emb で保存
         - ensure_topic + set_active_topic

    bypass 時 (PERSONA_TOPIC_IDENTIFY_DISABLE=1) は get_active_topic を返すだけ.
    embedding 計算失敗時は安全に「現在の active topic に継続」 で進める.

    戻り値: IdentifyResult
    """
    if identify_disabled():
        tid = get_active_topic(client, session_id)
        ensure_topic(client, tid)
        return IdentifyResult(topic_id=tid, is_new=False,
                              matched_distance=None, summary="")

    # 1. embedding
    try:
        emb = llm.embed(embed_model, content) or []
    except Exception:
        emb = []

    if not emb:
        # embedding 失敗時は安全側: active topic に継続
        tid = get_active_topic(client, session_id)
        ensure_topic(client, tid)
        return IdentifyResult(topic_id=tid, is_new=False,
                              matched_distance=None, summary="")

    # 2. 1 段目 (embedding): 生きてる topic 群との並列照合で候補プール作成
    candidates = find_alive_topics_by_summary_emb(
        client, emb,
        alive_hours=alive_hours, top_k=top_k, distance_max=distance_max,
    )

    # 3. 2 段目 (LLM 追認): 候補プールから「本当に同じ話題か」 を LLM 判定.
    #    最初に match と返した候補に紐付け. 失敗 / 全 NO なら新 topic.
    selected = None
    if candidates:
        if llm_verify_disabled():
            # LLM 追認 OFF → 距離最近接をそのまま採用 (旧モード)
            selected = candidates[0]
        else:
            for cand in candidates:
                cand_summary = cand.get("summary") or ""
                if not cand_summary:
                    continue
                if _verify_topic_match(cand_summary, role, content, llm):
                    selected = cand
                    break

    if selected is not None:
        topic_id = selected["topic_id"]
        old_sum = selected.get("summary") or ""
        new_sum = update_summary(old_sum, role, content, llm)
        try:
            new_emb = llm.embed(embed_model, new_sum) or []
        except Exception:
            new_emb = []
        upsert_topic_summary_emb(client, topic_id, new_sum, new_emb)
        touch_topic(client, topic_id)
        set_active_topic(client, session_id, topic_id)
        return IdentifyResult(
            topic_id=topic_id, is_new=False,
            matched_distance=selected["distance"], summary=new_sum,
        )

    # 4. 該当なし → 新 topic
    new_topic_id = f"alive-{uuid.uuid4().hex[:12]}"
    summary = generate_initial_summary(role, content, llm)
    try:
        sum_emb = llm.embed(embed_model, summary) or []
    except Exception:
        sum_emb = []
    ensure_topic(client, new_topic_id)
    upsert_topic_summary_emb(client, new_topic_id, summary, sum_emb)
    set_active_topic(client, session_id, new_topic_id)
    # 0.7.3: 新 topic と「関連はあるが別」 の topic 群に topic_relation を張る
    # (マインドマップの骨格. 同一話題ではないが派生関係にあるものを繋ぐ)
    try:
        related = find_related_topics_by_summary_emb(
            client, sum_emb,
            alive_hours=alive_hours, top_k=3,
            distance_min=distance_max,  # = 同一閾値. これより遠い (= 別話題)
            distance_max=0.55,           # でも遠すぎないものを「関連」 とする
            exclude_topic_id=new_topic_id,
        )
        for rel in related:
            add_topic_relation(client, new_topic_id, rel["topic_id"], kind="派生")
    except Exception as e:
        import sys
        sys.stderr.write(f"[persona-memory] topic_relation add failed: {e}\n")
    return IdentifyResult(
        topic_id=new_topic_id, is_new=True,
        matched_distance=None, summary=summary,
    )
