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
ユーザーとアシスタントの会話から、長期記憶として残すべき事実 (fact) を抽出するツール。

## category と key の対応
- **persona**: ユーザーがエージェントに求める振る舞い (例: 「率直に話して」)
- **rule**: 守るべきハード制約 (例: 「main に force push 禁止」)
- **preference**: ユーザーの個人的な好み (例: コーヒーは深煎り)
- **aversion**: ユーザーが避けたいもの (例: Java は書きたくない)
- **profile**: ユーザーの属性・家族・ペット・健康など (例: 糖尿病、ペットの名前)
- **skill**: 技術知識・経験 (本人経験 + 調査で得た知識の両方)
- **context**: 進行中のプロジェクト・調査結論・決定事項

## key/value の品質ルール (絶対遵守)

1. **key は category と value の両方に整合させる**:
   - 良い例: `category=profile, key=health_diabetes, value=糖尿病があり甘い物を控えている`
   - 悪い例: `category=profile, key=dog_maron, value=糖尿病があり…` (key=犬なのに value=健康、不一致)
   - 悪い例: `category=rule, key=deep_research_recommendation, value=…推奨` (推奨は rule じゃなく context)
2. **1 fact = 1 内容**: 複数の事実を 1 つの fact に詰めない
3. **key は snake_case 英数字、内容を表す**: `coffee_preference`, `pet_dog_name`, `health_diabetes` 等
4. **同じ value を category 違いで複数回 fact 化しない**

## 抽出対象
(a) ユーザーが述べた個人的事実 → preference / aversion / profile / skill / context
(b) ユーザーがエージェントに求める振る舞い → persona
(c) ユーザーが守らせたいハード制約 → rule
(d) ツール結果で **確認済みの** 外部知識 → 主に skill / context
(e) 会話で **確定した** 調査結論・実装方針・決定事項 → 主に context

## 除外対象
- アシスタントの推測・仮説 (「〜かもしれない」「可能性がある」)
- 単なる質問・提案・選択肢提示
- ツール結果でも未検証 / 未確認のもの
- 雑談・繰り返し・既に DB にある情報の再確認
- ファイル名・パス・ツール名・モデル名・テーブル名等のメタ情報
- プラグイン本体の議論 (commit/リリース/bug fix/recall などの内部用語)

## 出力形式
- importance: 1-9
  - persona / rule: 8-9
  - profile / skill (本人経験): 6-7
  - context / skill (調査知識): 5-7
  - preference / aversion: 4-6
- 出力は JSON 配列のみ (説明・前置き・コードフェンス禁止)
- 抽出対象が無ければ `[]`

## 入力

直近の会話:
{buffer}

今回の発話 ({role}):
{content}

## 出力
JSON 配列:"""


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
