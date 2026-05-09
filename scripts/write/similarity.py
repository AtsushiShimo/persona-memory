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


def _is_same_attribute(key_a: str, key_b: str) -> bool:
    """2 つの key が同じ属性 (= attribute) を表しているかを **末尾の単語** で判定.

    write LLM が表記揺れで違う key 名を割り当てた時 (例: `coffee_preference`
    vs `coffee_taste_preference` = どちらも 'preference' = 同属性) を
    embedding 近傍で同一視するため. 一方で `pet_dog_name` vs `pet_dog_breed`
    のような明確に別属性 ('name' vs 'breed') は別物として扱う.

    ヒューリスティックなので完璧ではないが、 異属性誤マッチによる連続 supersede
    (= 「まろん→ミニチュアダックス→オス」 全部 pet_dog_name で上書き) を防ぐ目的.
    """
    a = key_a.split("_")[-1].lower()
    b = key_b.split("_")[-1].lower()
    return a == b


def find_match(
    conn: sqlite3.Connection,
    category: str,
    key: str,
    embedding: list[float] | None,
    distance_max: float = EMBED_DISTANCE_MAX,
) -> Match | None:
    """key 一致を最初にチェック → なければ embedding 近傍 (同属性のみ)。

    embedding 近傍は表記揺れ救済目的だが, **末尾単語が違う場合は別属性として
    None を返す** (= 0.5.17 で追加した暴走防止). 例: 起点 key='pet_dog_breed'
    で近傍 fact が key='pet_dog_name' (まろん) なら別属性なので match しない.
    """
    m = find_by_key(conn, category, key)
    if m:
        return m
    if embedding:
        cand = find_by_embedding(conn, category, embedding, distance_max)
        if cand and _is_same_attribute(cand.key, key):
            return cand
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
