"""write LLM による fact 抽出。

input: 発話 (role + content) + 直近 BUFFER_N 発話
output: list[FactCandidate]

LLM は JSON 配列で抽出結果を返す。parse 失敗 / 空 → []。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from scripts.shared.ollama import LLMClient

WRITE_MODEL = os.environ.get(
    "PERSONA_WRITE_MODEL",
    os.environ.get("PERSONA_LIGHT_MODEL", "gemma3:4b"),
)

VALID_CATEGORIES = (
    "persona", "rule", "preference", "aversion", "profile", "skill", "context",
)


@dataclass
class FactCandidate:
    category: str
    key: str
    value: str
    importance: int

    def is_valid(self) -> bool:
        return (
            self.category in VALID_CATEGORIES
            and bool(self.key)
            and bool(self.value)
            and 1 <= self.importance <= 9
        )


_PROMPT_TEMPLATE = """\
あなたはユーザーとアシスタントの会話から、ユーザーの長期記憶として残すべき事実 (fact) を抽出するアシスタントです。

抽出ルール:
- category は次のいずれか: persona, rule, preference, aversion, profile, skill, context
  - persona: ユーザーがエージェント (あなた) に求める振る舞い・性格 (例: 「率直に指摘して」)
  - rule: 守るべきハード制約 (例: 「main に force push 禁止」)
  - preference: ユーザーの個人的な好み (例: コーヒーは深煎り)
  - aversion: ユーザーが避けたいもの (例: Java は書きたくない)
  - profile: ユーザーの属性 (役職、住居、家族構成など)
  - skill: 技術知識・経験
  - context: 進行中のプロジェクト・状況
- key は category 内で識別可能な英数字文字列 (snake_case 推奨、例: coffee_preference)
- value は事実本体を 1-2 行で簡潔に
- importance は 1-9 の整数。persona/rule は 8-9、profile/skill は 5-7、preference/aversion/context は 4-6 を目安
- 抽出すべき事実が無ければ空配列 [] を返す
- ユーザー自身の発言に基づく事実のみ抽出。アシスタントの推測や提案は対象外
- 出力は JSON 配列のみ (説明・前置き・コードフェンス禁止)

直近の会話:
{buffer}

今回の発話 ({role}):
{content}

JSON 配列で抽出結果を出力:"""


def build_prompt(role: str, content: str, buffer: list[dict]) -> str:
    if buffer:
        buf_lines = [f"[{m.get('role', '?')}] {m.get('content', '')}" for m in buffer]
        buf_text = "\n".join(buf_lines)
    else:
        buf_text = "(なし)"
    return _PROMPT_TEMPLATE.format(buffer=buf_text, role=role, content=content)


def parse_response(text: str) -> list[FactCandidate]:
    """LLM 出力 (期待: JSON 配列) を FactCandidate のリストに変換。"""
    if not text:
        return []
    # コードフェンス除去 (LLM が時々付けるので)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    # 配列部分を抽出 (前後ノイズ対策)
    m = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    out: list[FactCandidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            fc = FactCandidate(
                category=str(item.get("category", "")).strip().lower(),
                key=str(item.get("key", "")).strip(),
                value=str(item.get("value", "")).strip(),
                importance=int(item.get("importance", 5)),
            )
        except (TypeError, ValueError):
            continue
        if fc.is_valid():
            out.append(fc)
    return out


def extract_facts(
    role: str,
    content: str,
    buffer: list[dict],
    client: LLMClient,
    model: str = WRITE_MODEL,
) -> list[FactCandidate]:
    prompt = build_prompt(role, content, buffer)
    try:
        response = client.generate(model, prompt)
    except Exception:
        return []
    return parse_response(response)
