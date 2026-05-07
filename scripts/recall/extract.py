"""recall LLM による検索クエリ抽出.

発話 + 直近会話バッファから、過去 fact を引くためのキーワード/意図を取得。
短発話 (「OK」)・指示語 (「さっきの」) を吸収する。
"""
from __future__ import annotations

import json
import os
import re

from scripts.shared.ollama import LLMClient

RECALL_MODEL = os.environ.get(
    "PERSONA_RECALL_MODEL",
    os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b"),
)


_PROMPT_TEMPLATE = """\
あなたはユーザーの発話から、過去の記憶 (fact) を引き出すための検索キーワードを生成するアシスタントです。

ルール:
- 直近の会話を参考に、現発話の意図に関連しそうなキーワードを 1-3 個出す
- 指示語 (これ・あれ・さっきの・前のやつ・あの話) は直近会話で解決して具体名に変換
- 発話が短く意味のある内容が無い (例: 「OK」「了解」「ありがとう」) なら空配列 [] を返す
- 出力は JSON 配列のみ (説明・前置き・コードフェンス禁止)

直近の会話:
{buffer}

現発話:
{content}

JSON 配列で出力:"""


def build_prompt(content: str, buffer: list[dict]) -> str:
    if buffer:
        buf_lines = [f"[{m.get('role', '?')}] {m.get('content', '')}" for m in buffer]
        buf_text = "\n".join(buf_lines)
    else:
        buf_text = "(なし)"
    return _PROMPT_TEMPLATE.format(buffer=buf_text, content=content)


def parse_keywords(text: str) -> list[str]:
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
    return [str(k).strip() for k in data if isinstance(k, (str, int, float)) and str(k).strip()]


def extract_query_keywords(
    content: str,
    buffer: list[dict],
    client: LLMClient,
    model: str = RECALL_MODEL,
) -> list[str]:
    prompt = build_prompt(content, buffer)
    try:
        response = client.generate(model, prompt)
    except Exception:
        return []
    return parse_keywords(response)
