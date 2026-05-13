"""トピックタグ抽出 (write LLM とは別の light LLM コール).

設計:
- write LLM (heavy) は facts 抽出に集中させる. tag は別 light コールで抽出.
- 出力は短い名詞句 1-3 個. 「サイドバー UI」「メンション設計」 等の議題識別子.
- 雑談 / 単発相槌は空配列でよい.
- 既存タグの継続でも構わない (recall 側で重複は集約される).

PERSONA_TOPIC_DISABLE=1 / PERSONA_TAG_EXTRACT_DISABLE=1 で完全 bypass.
"""
from __future__ import annotations

import json
import os
import re

from scripts.shared.ollama import LLMClient

TAG_MODEL = os.environ.get(
    "PERSONA_TAG_MODEL",
    os.environ.get("PERSONA_LIGHT_MODEL", "gemma3:4b"),
)
# tag 抽出 prompt は短い (1-2k token). num_ctx を絞って KV cache を浪費しない.
TAG_NUM_CTX = int(os.environ.get("PERSONA_TAG_NUM_CTX", "4096"))


_PROMPT_TEMPLATE = """\
以下の発話の主題を表す**短い名詞句タグ** を 1-3 個 抽出してください.

判定基準:
- 設計・実装・調査の議題 → 機能名 / 概念名 / 固有名 (例: "サイドバー UI", "メンション設計", "Renju 命名")
- 同じ議題が継続している場合は前と同じタグを再出してよい
- 雑談 / 短い相槌 / 「OK」「了解」 等の閉じる発話は空配列 [] でよい
- タグは 15 字以内の名詞句. 動詞を含めない (例: ✗ "サイドバーを設計する" → ✓ "サイドバー設計")

直近の会話:
{buffer}

今回の発話 ({role}):
{content}

出力 (JSON 配列のみ, 説明・前置き・コードフェンス禁止):
[]
"""


def build_prompt(role: str, content: str, buffer: list[dict]) -> str:
    if buffer:
        buf_text = "\n".join(f"[{m.get('role', '?')}] {m.get('content', '')}" for m in buffer)
    else:
        buf_text = "(なし)"
    return _PROMPT_TEMPLATE.format(buffer=buf_text, role=role, content=content)


def parse_tags(text: str) -> list[str]:
    if not text:
        return []
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    m = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for x in data:
        if not isinstance(x, (str, int, float)):
            continue
        s = str(x).strip()
        if not s:
            continue
        if len(s) > 30:  # 規定 15 字超でも 30 で頭打ちにして保存 (LLM ばらつき緩和)
            s = s[:30]
        out.append(s)
        if len(out) >= 3:
            break
    return out


def extract_tags(
    role: str,
    content: str,
    buffer: list[dict],
    client: LLMClient,
    model: str = TAG_MODEL,
) -> list[str]:
    """LLM に tag 抽出させて list[str] を返す. 失敗時は空."""
    prompt = build_prompt(role, content, buffer)
    try:
        response = client.generate(model, prompt, num_ctx=TAG_NUM_CTX)
    except Exception:
        return []
    return parse_tags(response)
