"""embedding ベクトルの sqlite-vec 用 BLOB pack/unpack."""
from __future__ import annotations

import struct


def pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))
