"""LLM で 1 episode から「議論ノード」 と関係性を抽出 (0.7.3 拡張).

0.6.x の単純な「直前ノードへの関係性」 から、 0.7.3 では:
- リトライ機能 (LLM が抽出失敗を返した場合のフォールバック)
- 同 topic 内の候補ノード群を渡し、 **離れたノード** への意味的エッジも生成
  (例: 3 個前の question への answer 紐付け)
- 抽出を積極的に行うようプロンプト調整

抽出結果は NodeCandidate. relations は:
- prev_relation: 直前ノードへの関係 (旧来通り)
- target_id + target_relation: 離れた特定ノードへの関係 (新規)
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from scripts.shared.ollama import LLMClient

GRAPH_MODEL = os.environ.get(
    "PERSONA_GRAPH_MODEL",
    os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b"),
)
GRAPH_NUM_CTX = int(os.environ.get("PERSONA_GRAPH_NUM_CTX", "16384"))
GRAPH_RETRY = int(os.environ.get("PERSONA_GRAPH_RETRY", "2"))

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
    prev_relation: str | None = None  # 直前 node への edge kind
    # 0.7.3: 離れたノードへの edge. target_id は candidate_nodes の id を参照.
    target_id: int | None = None
    target_relation: str | None = None
    # 抽出時の補助情報 (debug 用)
    extracted_attempt: int = field(default=0)


_PROMPT_TEMPLATE = """\
あなたは会話を「議論の流れ」 として記録する補助です.
今回の発話を解析し、 主張 / 提案 / 決定 / 観察 / 質問 / 回答 等が含まれていれば
**1 つの議論ノード** として抽出してください.

**抽出方針 (積極的に):**
- 発話に主題語が含まれていれば抽出する. 短くても抽出する.
- 「相槌」「単純な感謝」「『はい』だけ」 のような完全な空発話は null.
- それ以外は **必ず 1 ノード抽出** (kind を最も近いものに割り当てる).
- 迷ったら observation で抽出する.

抽出対象 kind:
- topic: 新しい論点・話題の開始
- option: 検討案 (まだ確定していない選択肢)
- decision: 採用・確定の判断
- retraction: 撤回
- rationale: 理由・根拠
- observation: 観察・現状報告 / 状況確認 / 共通認識
- question: ユーザーやモデルへの問いかけ
- answer: 質問への回答

state:
- proposed (検討中) / accepted (採用) / rejected (却下) / superseded (上書きされた) / observed (中立観察)

{prev_section}{candidates_section}
**直前ノードへの関係性 prev_relation** — 直前ノードに対して今回の発話が
どういう関係か. **直前ノードが存在し、 同じ話題内であれば必ず何かしらの関係を返す**.
- 賛同 / 反論 / 派生 / 決定 (option→decision の採用) / 次バトン (次論点投げ) /
  観察 / 結論 / 質問 / 回答 / 撤回
- どれにも当てはまらない場合でも、 同一話題なら「派生」 (= 議論の続き) を返す.
- null を返してよいのは: 直前ノード自体が無い場合 / 明らかに別話題に切り替わった場合のみ.

**離れたノードへの関係 target_id / target_relation** — 上の「同 topic 内の候補ノード」 群
の中で、 今回の発話が直接呼応している過去ノードがあれば、 その id と関係を返す.
- 例: 今回が "answer" で、 候補に "#3 question 〇〇" があれば、 target_id=3, target_relation=回答.
- 例: 今回が "decision" で、 候補に "#5 option △△" があり今回がそれを採用するなら target_id=5, target_relation=決定.
- 該当無し / 直前ノードだけが対象 → target_id=null, target_relation=null.
- 直前ノードと target_id は重複しても良い (両方記録される).

直近の会話:
{buffer}

今回の発話 ({role}):
{content}

出力 JSON のみ (説明・前置き・コードフェンス禁止):
- 抽出対象なし: `null`
- 1 ノード抽出: `{{"kind": "...", "title": "...", "state": "...", "content": "...", "prev_relation": "..." | null, "target_id": <int> | null, "target_relation": "..." | null}}`

title は 20 字以内. content は 100 字以内 (任意).
"""


def build_prompt(
    role: str,
    content: str,
    buffer: list[dict],
    prev_node: dict | None = None,
    candidate_nodes: list[dict] | None = None,
) -> str:
    if buffer:
        buf = "\n".join(f"[{m.get('role','?')}] {m.get('content','')}" for m in buffer)
    else:
        buf = "(なし)"
    if prev_node:
        prev_section = (
            f"**直前ノード** (同じ話題で直前に記録された議論ノード):\n"
            f"  #{prev_node.get('id', '?')} [{prev_node.get('kind', '?')}/"
            f"{prev_node.get('state', '?')}] {prev_node.get('title', '')}\n\n"
        )
    else:
        prev_section = "**直前ノード**: なし (この発話が流れの先頭)\n\n"
    if candidate_nodes:
        lines = ["**同 topic 内の候補ノード** (離れた関係 target_id 用):"]
        for c in candidate_nodes:
            cid = c.get("id", "?")
            kind = c.get("kind", "?")
            title = c.get("title", "")
            state = c.get("state", "")
            lines.append(f"  #{cid} [{kind}/{state}] {title}")
        candidates_section = "\n".join(lines) + "\n\n"
    else:
        candidates_section = ""
    return _PROMPT_TEMPLATE.format(
        buffer=buf, role=role, content=content,
        prev_section=prev_section, candidates_section=candidates_section,
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
    # target_id / target_relation
    target_id = data.get("target_id")
    if isinstance(target_id, str):
        if target_id.strip().lower() == "null":
            target_id = None
        else:
            try:
                target_id = int(target_id.strip())
            except ValueError:
                target_id = None
    elif not isinstance(target_id, int):
        target_id = None
    target_rel = data.get("target_relation")
    if isinstance(target_rel, str):
        target_rel = target_rel.strip()
        if target_rel.lower() == "null" or not target_rel:
            target_rel = None
        elif target_rel not in VALID_RELATIONS:
            target_rel = None
    else:
        target_rel = None
    # target_id だけあって relation が無い場合は破棄
    if target_id is not None and target_rel is None:
        target_id = None
    return NodeCandidate(
        kind=kind, title=title, state=state, content=content,
        prev_relation=prev, target_id=target_id, target_relation=target_rel,
    )


def extract_node_with_relation(
    role: str,
    content: str,
    buffer: list[dict],
    client: LLMClient,
    model: str = GRAPH_MODEL,
    prev_node: dict | None = None,
    candidate_nodes: list[dict] | None = None,
    retry: int = GRAPH_RETRY,
) -> NodeCandidate | None:
    """LLM 抽出. 失敗時は最大 retry 回 retry. 短いがゼロでない発話は最低限抽出する.

    candidate_nodes: 同 topic 内の最近 N 件のノード (新発話との関連付け候補).
      [{"id": int, "kind": str, "title": str, "state": str}, ...]
    """
    prompt = build_prompt(
        role, content, buffer,
        prev_node=prev_node, candidate_nodes=candidate_nodes,
    )
    last_response = ""
    for attempt in range(retry + 1):
        try:
            response = client.generate(model, prompt, num_ctx=GRAPH_NUM_CTX)
        except Exception:
            response = ""
        last_response = response
        nc = parse_node(response)
        if nc is not None:
            nc.extracted_attempt = attempt
            return nc
        # null だった場合は短文相槌の可能性. content 長で fallback 判定.
        stripped = (content or "").strip()
        if len(stripped) < 8:
            return None  # 真に短い相槌は諦める
        # それ以上長い発話で抽出失敗 → retry (プロンプト微変更しない、 単に再試行)
    # 最終フォールバック: 内容から最小限のノードを構築
    stripped = (content or "").strip()
    if len(stripped) >= 8:
        title = stripped[:20]
        return NodeCandidate(
            kind="observation",
            title=title,
            state="observed",
            content=stripped[:200],
            prev_relation="派生" if prev_node else None,
            extracted_attempt=retry + 1,
        )
    return None
