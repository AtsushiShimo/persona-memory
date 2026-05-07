"""エスカレーション条件判定 (§3.1 の 3 OR 条件)."""
from __future__ import annotations

import os

INPUT_TOKEN_THRESHOLD = int(os.environ.get("PERSONA_ESCALATE_INPUT_TOKENS", "2000"))
IMPORTANCE_THRESHOLD = int(os.environ.get("PERSONA_ESCALATE_IMPORTANCE", "8"))


def estimate_tokens(text: str) -> int:
    """ざっくり token 数 (日本語混在)。char/2 を上限近似。"""
    if not text:
        return 0
    return len(text) // 2


def should_escalate(
    input_text: str = "",
    importance: int = 0,
    uncertain: bool = False,
) -> str | None:
    """エスカレーション理由を返す。None = 不要。

    OR 条件:
    - 入力トークン総量 > INPUT_TOKEN_THRESHOLD
    - importance >= IMPORTANCE_THRESHOLD
    - uncertain == True (LLM が判定不能)
    """
    if estimate_tokens(input_text) > INPUT_TOKEN_THRESHOLD:
        return "long_input"
    if importance >= IMPORTANCE_THRESHOLD:
        return "high_importance"
    if uncertain:
        return "uncertainty"
    return None
