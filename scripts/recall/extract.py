"""recall LLM による検索クエリ解析.

発話 + 直近会話バッファから:
- 検索キーワード (1-3 個)
- 過去の会話ログを探す必要があるか (「さっき」 等の指示語 / 履歴系の意図を LLM が判断)

を **同じ LLM 呼び出し** で取得する。trigger 検出は正規表現ではなく LLM
判断 (日本語の表現バリエーションは regex で網羅できないため、仕様書 §5.1 の
「LLM が意図を抽出」 に従う)。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from scripts.shared.ollama import LLMClient

RECALL_MODEL = os.environ.get(
    "PERSONA_RECALL_MODEL",
    os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b"),
)


@dataclass
class QueryAnalysis:
    keywords: list[str]
    search_history: bool  # 過去会話ログを引くべきか (LLM が判断)


_PROMPT_TEMPLATE = """\
あなたはユーザーの発話を解析し、関連する記憶を引き出すための情報を生成するアシスタントです。

タスク:
1. 直近の会話を参考に、現発話の意図に関連しそうなキーワードを 1-3 個出す
   - 指示語 (これ・あれ・さっき・前の・あの話・このまえ) は直近会話で解決して具体名に変換
   - **短い発話 (「OK」「了解」「進める」「採用」「うん」「ありがとう」 等) でも空配列にせず、
     直前の議題を主題として keyword 化する**。例: 直前が「Phase 1 で進めますか?」 への
     「OK」 なら keyword は ["Phase 1"]。直前が雑談で何も無ければ無理に作らず空でよい
2. 過去の会話ログを引く必要があるか判定 (search_history)
   - true: ユーザーが「過去に話したこと」 を思い出そうとしている時
     例: 「さっき何話した?」「以前の議論」「前にも言ったけど」「この前の話」「あれ覚えてる?」
   - false: 一般的な質問・新しい話題・指示・フィードバック
   - **会話ログ参照の意図を LLM として判断する** (regex 検出ではない)

直近の会話:
{buffer}

現発話:
{content}

出力は JSON のみ (説明・前置き・コードフェンス禁止):
{{"keywords": ["..."], "search_history": true|false}}
"""


def build_prompt(content: str, buffer: list[dict]) -> str:
    if buffer:
        buf_lines = [f"[{m.get('role', '?')}] {m.get('content', '')}" for m in buffer]
        buf_text = "\n".join(buf_lines)
    else:
        buf_text = "(なし)"
    return _PROMPT_TEMPLATE.format(buffer=buf_text, content=content)


def parse_analysis(text: str) -> QueryAnalysis:
    """LLM 出力 (期待: JSON object) を QueryAnalysis に変換. パース失敗 → 空."""
    empty = QueryAnalysis(keywords=[], search_history=False)
    if not text:
        return empty
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return empty
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return empty
    if not isinstance(data, dict):
        return empty
    raw_keywords = data.get("keywords") or []
    if not isinstance(raw_keywords, list):
        raw_keywords = []
    keywords = [
        str(k).strip()
        for k in raw_keywords
        if isinstance(k, (str, int, float)) and str(k).strip()
    ]
    search_history = bool(data.get("search_history", False))
    return QueryAnalysis(keywords=keywords, search_history=search_history)


def analyze_query(
    content: str,
    buffer: list[dict],
    client: LLMClient,
    model: str = RECALL_MODEL,
) -> QueryAnalysis:
    from scripts.debug.recall_log import (
        is_enabled as _debug_enabled,
        log_extract_prompt,
        log_extract_response,
    )
    prompt = build_prompt(content, buffer)
    if _debug_enabled():
        log_extract_prompt(prompt)
    try:
        response = client.generate(model, prompt)
    except Exception:
        if _debug_enabled():
            log_extract_response("(generate failed)")
        return QueryAnalysis(keywords=[], search_history=False)
    if _debug_enabled():
        log_extract_response(response)
    return parse_analysis(response)


# ── back-compat: 旧 API (キーワードのみ) ────────────────────────────────────

def parse_keywords(text: str) -> list[str]:
    """旧テスト用 back-compat. 新コードは parse_analysis を使う."""
    return parse_analysis(text).keywords


def extract_query_keywords(
    content: str,
    buffer: list[dict],
    client: LLMClient,
    model: str = RECALL_MODEL,
) -> list[str]:
    """旧 API. 新コードは analyze_query を使う."""
    return analyze_query(content, buffer, client, model).keywords
