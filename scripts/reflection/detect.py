"""怒気検知 (反省モード発火源).

2 段判定:
- 1 段目 (keyword): 怒気・否定・叱責を示すフレーズの正規表現.
  該当しなければ即 False 返し (常会話の通常 path を遅延させない).
- 2 段目 (light LLM): keyword 候補を gemma3:4b に投げて
  「怒気あり / 強い不満 / 通常」 の 3 値判定.
  「怒気あり / 強い不満」 が **取りこぼし禁止** で反省モード発火.
  「通常」 のみスルー (= 強めバイアス).

env:
- PERSONA_ANGER_DETECT_DISABLE=1: 反省モード機能を全停止 (緊急 revert)
- PERSONA_ANGER_LLM_DISABLE=1: LLM 2 段目 skip. keyword だけで発火
  (= 更に強め. 低レイテンシ環境用)
- PERSONA_ANGER_VERIFY_MODEL: 2 段目モデル (default = gemma3:4b)
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


def disabled() -> bool:
    return os.environ.get("PERSONA_ANGER_DETECT_DISABLE", "").strip() == "1"


def llm_disabled() -> bool:
    return os.environ.get("PERSONA_ANGER_LLM_DISABLE", "").strip() == "1"


# 1 段目 keyword. 取りこぼし禁止の方針なので広めに取る.
# 誤発火は LLM 2 段目で吸収する.
_ANGER_KEYWORD_RE = re.compile(
    "|".join([
        # 直接的叱責
        r"ちげー", r"違うよ", r"違うって", r"間違って", r"間違え",
        r"ダメ", r"駄目", r"やめて", r"やめろ", r"やめろよ",
        # 罵倒・強い不満
        r"クソ", r"バカ", r"馬鹿", r"あほ", r"アホ",
        r"ふざけ", r"いい加減", r"ありえ",
        # 致命度示唆
        r"致命", r"最悪",
        # 反復叱責
        r"何回", r"何度", r"いつも", r"何度言",
        # 強い疑問 + 否定
        r"なんでだよ", r"なんで.{0,8}できない", r"なんで.{0,8}しない",
        # 認識合わせ要求
        r"認識.{0,4}違", r"認識.{0,4}合",
        # 弱い不満 (= 言い方の弱い指示も拾う方針)
        r"おかしい", r"おかしいよ", r"視野.{0,2}狭",
        r"スコープ.{0,2}違", r"分かんない", r"分からない",
        r"わかんない", r"わかんねー", r"わかんねぇ",
        r"意味.{0,2}分から", r"意味.{0,2}わかん",
        # 命令口調の修正要求
        r"治して", r"直して(?!.*ください)", r"勝手に",
    ]),
    re.IGNORECASE,
)


_VERIFY_PROMPT = """\
以下はユーザーから AI アシスタントへの発話です. アシスタントに対する
**怒り・強い不満・叱責** が含まれているかを判定してください.

判定方針 (取りこぼし禁止. 迷ったら anger):
- "anger": 怒り / 叱責 / 強い不満 / 「違う」「やめろ」「分からない」 等の否定.
  言い方が穏やかでも、 アシスタントの振る舞いを修正させる意図があれば anger.
- "neutral": 通常の質問・依頼・相槌・前向きな指示.

ユーザー発話:
{content}

JSON のみ (説明・前置き・コードフェンス禁止):
{{"answer": "anger" | "neutral", "reason": "1 行"}}
"""


def _llm_verify(content: str, llm: LLMClient, model: str = VERIFY_MODEL) -> bool:
    """LLM に怒気判定. 失敗時は True (= 安全側 = 取りこぼし禁止)."""
    if not content.strip():
        return False
    prompt = _VERIFY_PROMPT.format(content=content[:600])
    try:
        response = llm.generate(model, prompt, num_ctx=VERIFY_NUM_CTX)
    except Exception:
        return True  # 失敗 = 反省モード側に倒す
    if not response:
        return True
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip(),
                     flags=re.MULTILINE)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return True
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return True
    if not isinstance(data, dict):
        return True
    return str(data.get("answer", "")).strip().lower() == "anger"


def detect_anger(content: str, llm: LLMClient | None = None) -> tuple[bool, str]:
    """発話に怒気が含まれるかを判定.

    戻り値: (anger?, matched_phrase or "")
    - matched_phrase: 1 段目 keyword でマッチした最初のフレーズ.
      LLM 単独発火時は空文字.
    """
    if disabled() or not content:
        return False, ""
    m = _ANGER_KEYWORD_RE.search(content)
    if not m:
        return False, ""
    phrase = m.group(0)
    # 1 段目で候補化. LLM disabled なら即発火 (= 更に強め).
    if llm_disabled() or llm is None:
        return True, phrase
    # 2 段目: LLM 確認
    if _llm_verify(content, llm):
        return True, phrase
    return False, ""
