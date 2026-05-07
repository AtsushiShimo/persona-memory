"""機密情報検出 — 両 hook (UserPromptSubmit / Stop) の入口で同期実行。

検出時の挙動 (spec §4.5):
- raw 保存 / fact 抽出 / recall を全スキップ
- stderr 警告 + Keychain 退避ガイダンス
- override 機構 (allow-last / allowlist) は post-MVP
"""
from __future__ import annotations

import math
import re

_SECRET_PATTERNS = re.compile(
    r"(?:"
    r"sk-proj-[A-Za-z0-9\-_]{20,}"
    r"|sk-[A-Za-z0-9\-_]{20,}"
    r"|ghp_[A-Za-z0-9]{36,}"
    r"|gh[ousr]_[A-Za-z0-9]{36,}"
    r"|AKIA[A-Z0-9]{16}"
    r"|sk_live_[A-Za-z0-9]+"
    r"|sk_test_[A-Za-z0-9]+"
    r"|rk_live_[A-Za-z0-9]+"
    r"|xoxb-[A-Za-z0-9\-]+"
    r"|xoxp-[A-Za-z0-9\-]+"
    r"|ya29\.[A-Za-z0-9\-_]+"
    r"|AIza[A-Za-z0-9\-_]{35}"
    r"|eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+"
    r")"
)

ENTROPY_TOKEN_MIN_LEN = 32
ENTROPY_THRESHOLD = 4.2


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    return -sum((f / len(s)) * math.log2(f / len(s)) for f in freq.values())


def _mask(val: str) -> str:
    if len(val) <= 10:
        return val[:2] + "..." + val[-2:]
    return val[:6] + "..." + val[-4:]


def detect_secrets(text: str) -> list[str]:
    """検出した機密のマスク済みプレビューを返す。空リスト = clean。"""
    found: list[str] = []
    if not text:
        return found

    for m in _SECRET_PATTERNS.finditer(text):
        found.append(_mask(m.group()))

    # 既知パターン非ヒット時のみエントロピー検査 (誤検出を抑える)
    if not found:
        for token in re.findall(r"[A-Za-z0-9+/=_\-]{" + str(ENTROPY_TOKEN_MIN_LEN) + r",}", text):
            if _shannon_entropy(token) > ENTROPY_THRESHOLD:
                found.append(_mask(token))

    return found


def warning_message(found: list[str]) -> str:
    """stderr に出す警告文 + Keychain 退避ガイダンス。"""
    masked = ", ".join(found)
    return (
        "⚠️  persona-memory: 機密 (API キー / 高エントロピートークン) を検出。\n"
        f"   検出箇所: {masked}\n"
        "   この発話は永続化も recall も行いません。\n\n"
        "   安全な手順 (推奨):\n"
        "     1. .env に保存する → MY_KEY=<値>\n"
        "     2. 会話では「.env の MY_KEY」 とキー名だけ伝える\n"
    )
