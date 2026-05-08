"""類似 fact の検出 (key + embedding ハイブリッド)."""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from scripts.shared.embedding import pack

EMBED_DISTANCE_MAX = 0.4  # cosine 距離スケール、近傍 fact 判定の閾値 (テストで調整可能)


@dataclass
class Match:
    fact_id: int
    category: str
    key: str
    value: str
    importance: int
    method: str  # 'key' or 'embedding'


def _cosine_distance(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - (dot / (na * nb))


def find_by_key(conn: sqlite3.Connection, category: str, key: str) -> Match | None:
    row = conn.execute(
        "SELECT id, category, key, value, importance "
        "FROM facts WHERE category = ? AND key = ? AND status = 'active'",
        (category, key),
    ).fetchone()
    if not row:
        return None
    return Match(
        fact_id=row[0], category=row[1], key=row[2],
        value=row[3], importance=row[4], method="key",
    )


def find_by_embedding(
    conn: sqlite3.Connection,
    category: str,
    embedding: list[float],
    distance_max: float = EMBED_DISTANCE_MAX,
    k: int = 10,
) -> Match | None:
    """同 category 内で近傍 active fact を 1 件返す。なければ None。

    sqlite-vec の MATCH は別 CTE で近傍 ID + distance を取り、外側で facts
    に JOIN + WHERE で active + 同 category に絞る。
    """
    if not embedding:
        return None
    blob = pack(embedding)
    row = conn.execute(
        """
        WITH knn AS (
          SELECT fact_id, distance
          FROM fact_embeddings
          WHERE embedding MATCH ? AND k = ?
        )
        SELECT f.id, f.category, f.key, f.value, f.importance, knn.distance
        FROM knn
        JOIN facts f ON f.id = knn.fact_id
        WHERE f.status = 'active' AND f.category = ?
        ORDER BY knn.distance
        LIMIT 1
        """,
        (blob, k, category),
    ).fetchone()
    if not row:
        return None
    if row[5] > distance_max:
        return None
    return Match(
        fact_id=row[0], category=row[1], key=row[2],
        value=row[3], importance=row[4], method="embedding",
    )


def find_match(
    conn: sqlite3.Connection,
    category: str,
    key: str,
    embedding: list[float] | None,
    distance_max: float = EMBED_DISTANCE_MAX,
) -> Match | None:
    """key 一致を最初にチェック → なければ embedding 近傍。"""
    m = find_by_key(conn, category, key)
    if m:
        return m
    if embedding:
        return find_by_embedding(conn, category, embedding, distance_max)
    return None


# ── 補強 vs 変更 の判定 (heuristic、phase 6 で Claude エスカレーション追加) ─

def is_reinforcement(old_value: str, new_value: str) -> bool:
    """文字列がほぼ同じなら補強、違えば変更。phase 3 は単純な heuristic。

    - 完全一致 → 補強
    - 一方が他方の部分文字列 → 補強
    - 文字 Jaccard が 0.7 以上 → 補強
    - それ以外 → 変更
    """
    a, b = old_value.strip(), new_value.strip()
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return True

    sa = set(a)
    sb = set(b)
    if not sa or not sb:
        return False
    jaccard = len(sa & sb) / len(sa | sb)
    return jaccard >= 0.7
