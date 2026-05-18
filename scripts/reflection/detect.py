"""怒気検知 (反省モード発火源).

0.8.5 LLM-only 版: 1 段目正規表現フィルタは取りこぼし温床として撤廃.
全発話を `gemma3:4b` 相当の light LLM に投げ、 怒り / 強い不満 / 叱責 を
取りこぼし禁止で判定する.

検知の on/off は Cozo の `reflection_state.detection_enabled` (DB persisted)
で動的に管理する — slash command `/persona-memory:reflection-off` か MCP tool
`set_anger_detection(enabled=False)` で切替. dispatch は hook 層 (caller) が
担い、 本モジュールは「呼ばれたら判定する」 純粋ロジックに徹する.

env:
- PERSONA_ANGER_VERIFY_MODEL: 判定モデル (default = gemma3:4b)
- PERSONA_ANGER_VERIFY_NUM_CTX: num_ctx (default = 4096)

判定方針 (= ご主人様確定):
- anger_detection_sensitivity = 強めに倒す. 「言い方の弱い指示・不満」 も拾う.
- LLM 失敗時は anger 側に倒す (= 取りこぼし禁止).
"""
from __future__ import annotations

import json
import os
import re

from scripts.shared.ollama import LLMClient

VERIFY_MODEL = os.environ.get(
    "PERSONA_ANGER_VERIFY_MODEL",
    os.environ.get("PERSONA_LIGHT_MODEL", "gemma3:4b"),
)
VERIFY_NUM_CTX = int(os.environ.get("PERSONA_ANGER_VERIFY_NUM_CTX", "4096"))


_VERIFY_PROMPT = """\
以下はユーザーから AI アシスタントへの発話です. アシスタントに対する
**怒り・強い不満・叱責** が含まれているかを判定してください.

判定方針 (取りこぼし禁止. 迷ったら anger):
- "anger": 怒り / 叱責 / 強い不満 / 「違う」「やめろ」「分からない」「おかしい」
  「なんで〜できない」 等の否定. 言い方が穏やかでも、 アシスタントの振る舞いを
  修正させる意図があれば anger.
- "neutral": 通常の質問・依頼・相槌・前向きな指示・新規話題の提示.

ユーザー発話:
{content}

JSON のみ (説明・前置き・コードフェンス禁止):
{{"answer": "anger" | "neutral", "phrase": "該当箇所の短い引用 (anger の時のみ. 40 文字以内)"}}
"""


def _llm_judge(
    content: str, llm: LLMClient, model: str = VERIFY_MODEL,
) -> tuple[bool, str]:
    """LLM に怒気判定 + 該当フレーズ抽出.

    戻り値: (anger?, phrase)
    失敗時は (True, content[:40]) — 取りこぼし禁止方針.
    """
    if not content.strip():
        return False, ""
    snippet = content[:40].replace("\n", " ").strip()
    prompt = _VERIFY_PROMPT.format(content=content[:600])
    try:
        response = llm.generate(model, prompt, num_ctx=VERIFY_NUM_CTX)
    except Exception:
        return True, snippet
    if not response:
        return True, snippet
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip(),
                     flags=re.MULTILINE)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return True, snippet
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return True, snippet
    if not isinstance(data, dict):
        return True, snippet
    is_anger = str(data.get("answer", "")).strip().lower() == "anger"
    if not is_anger:
        return False, ""
    phrase = str(data.get("phrase", "")).strip()
    return True, (phrase or snippet)[:40]


def detect_anger(content: str, llm: LLMClient | None = None) -> tuple[bool, str]:
    """発話に怒気が含まれるかを判定 (LLM-only).

    戻り値: (anger?, phrase)
    - phrase: LLM が抽出した該当フレーズ. 抽出失敗時は発話冒頭 40 文字.
    - 空文字 / llm=None の時は (False, "") を返す.

    呼出側の責務: 検知 on/off の dispatch は hook 層が
    `state.is_detection_enabled(client)` を読んで行う. 本関数は disable 経路
    を一切持たない (= 並走負債を作らない設計).
    """
    if not content:
        return False, ""
    if llm is None:
        return False, ""
    return _llm_judge(content, llm)
