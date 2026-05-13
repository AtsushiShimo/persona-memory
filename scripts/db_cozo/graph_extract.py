"""LLM で 1 episode から「議論ノード」 と「直前ノードへの関係性」 を抽出.

旧 scripts/write/extract.py の節点抽出を Cozo モデル向けに拡張:
- 出力に `prev_relation` を追加: 「賛同 / 反論 / 派生 / 決定 / 次バトン /
  観察 / 結論 / 質問 / 回答」 or null
- prev_relation が非 null なら、 同 topic 内の直前ノードに対して edge を張る
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from scripts.shared.ollama import LLMClient

GRAPH_MODEL = os.environ.get(
    "PERSONA_GRAPH_MODEL",
    os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b"),
)
GRAPH_NUM_CTX = int(os.environ.get("PERSONA_GRAPH_NUM_CTX", "16384"))

VALID_KINDS = (
    "topic", "option", "decision", "retraction",
    "rationale", "observation", "question", "answer",
)
VALID_STATES = (
    "proposed", "accepted", "rejected", "superseded", "observed",
)
VALID_RELATIONS = (
    "賛同", "反論", "派生", "決定", "次バトン",
    "観察", "結論", "質問", "回答", "撤回",
)


@dataclass
class NodeCandidate:
    kind: str
    title: str
    state: str = "proposed"
    content: str | None = None
    prev_relation: str | None = None  # 直前 node への edge kind. None なら edge 張らず.


_PROMPT_TEMPLATE = """\
あなたは会話を「議論の流れ」 として記録する補助です.
今回の発話を解析し、 主張 / 提案 / 決定 / 観察 / 質問 / 回答 等が含まれていれば
**1 つの議論ノード** として抽出してください. 雑談・相槌・繰り返しは抽出不要.

抽出対象 kind:
- topic: 新しい論点・話題
- option: 検討案 (まだ確定していない選択肢)
- decision: 採用・確定の判断
- retraction: 撤回
- rationale: 理由・根拠
- observation: 観察・現状報告 (例: 「次論点 4 つ提示」)
- question: ユーザーやモデルへの問いかけ
- answer: 直前の質問への回答

state:
- proposed (検討中) / accepted (採用) / rejected (却下) / superseded (上書きされた) / observed (中立観察)

{prev_section}
**直前ノードへの関係性 prev_relation** — 上の「直前ノード」 に対して今回の発話が
どういう関係か:
- 賛同 / 反論 / 派生 / 決定 (option→decision の採用) / 次バトン (次論点投げ) /
  観察 / 結論 / 質問 / 回答 / 撤回
- 直前ノードが無い、 または明らかに別話題なら null

直近の会話:
{buffer}

今回の発話 ({role}):
{content}

出力 JSON のみ (説明・前置き・コードフェンス禁止):
- 抽出対象なし: `null`
- 1 ノード抽出: `{{"kind": "...", "title": "...", "state": "...", "content": "...", "prev_relation": "..." | null}}`

title は 20 字以内. content は 100 字以内 (任意).
"""


def build_prompt(
    role: str, content: str, buffer: list[dict],
    prev_node: dict | None = None,
) -> str:
    if buffer:
        buf = "\n".join(f"[{m.get('role','?')}] {m.get('content','')}" for m in buffer)
    else:
        buf = "(なし)"
    if prev_node:
        prev_section = (
            f"**直前ノード** (同じ話題で直前に記録された議論ノード):\n"
            f"  #{prev_node.get('id', '?')} [{prev_node.get('kind', '?')}/"
            f"{prev_node.get('state', '?')}] {prev_node.get('title', '')}\n"
        )
    else:
        prev_section = "**直前ノード**: なし (この発話が流れの先頭)\n"
    return _PROMPT_TEMPLATE.format(
        buffer=buf, role=role, content=content, prev_section=prev_section,
    )


def parse_node(text: str) -> NodeCandidate | None:
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    if cleaned.lower() in ("null", ""):
        return None
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "").strip()
    title = str(data.get("title") or "").strip()[:30]
    if not kind or not title:
        return None
    if kind not in VALID_KINDS:
        return None
    state = str(data.get("state") or "proposed").strip()
    if state not in VALID_STATES:
        state = "proposed"
    content = data.get("content")
    if content is not None:
        content = str(content).strip()[:200] or None
    prev = data.get("prev_relation")
    if isinstance(prev, str):
        prev = prev.strip()
        if prev.lower() == "null" or not prev:
            prev = None
        elif prev not in VALID_RELATIONS:
            prev = None
    else:
        prev = None
    return NodeCandidate(
        kind=kind, title=title, state=state, content=content, prev_relation=prev,
    )


def extract_node_with_relation(
    role: str, content: str, buffer: list[dict],
    client: LLMClient, model: str = GRAPH_MODEL,
    prev_node: dict | None = None,
) -> NodeCandidate | None:
    prompt = build_prompt(role, content, buffer, prev_node=prev_node)
    try:
        response = client.generate(model, prompt, num_ctx=GRAPH_NUM_CTX)
    except Exception:
        return None
    return parse_node(response)
