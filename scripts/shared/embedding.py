"""embedding ベクトルの sqlite-vec 用 BLOB pack/unpack + 入力テキスト前処理."""
from __future__ import annotations

import struct

# nomic-embed-text のコンテキスト窓 (~2048 token) を超えると Ollama が
# 500 を返す。 日本語は 1 文字 ≒ 1-1.5 token のため、 3000 文字で切ると
# 安全余裕付きで収まる。 長い episode (例: tool_result の貼付ログ、 大型
# Bash 出力等) は embedding 計算用にだけ切り詰めて「先頭文脈の代表表現」
# を作る; raw content は DB に無傷で残るので情報は失われない.
EMBED_TEXT_MAX_CHARS = 3000


def truncate_for_embedding(text: str, max_chars: int = EMBED_TEXT_MAX_CHARS) -> str:
    """embedding 投入前に text を安全長に切り詰める.

    None や空文字はそのまま空文字で返し、 上流の「空 vector」 経路に
    乗せる (= 500 エラーで停止させない). すでに短いものは無加工.
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))
