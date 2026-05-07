"""facts の検索とランキング.

仕様 §5.4: relevance × recency × importance × access_count
- relevance:    1 / (1 + distance)            (近いほど 1 に近づく)
- recency:      exp(-λ × age_days)             (新しいほど 1)
- importance:   importance / 9                 (1-9 を 0..1 に正規化)
- access_count: log(1 + access_count) / 5     (頻出ほど大きい、上限近く 1)
"""
from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from scripts.shared.embedding import pack

JST = timezone(timedelta(hours=9))
RECENCY_LAMBDA = float(os.environ.get("PERSONA_RECALL_RECENCY_LAMBDA", "0.05"))
TOP_K_PER_KEYWORD = int(os.environ.get("PERSONA_RECALL_TOP_K_PER_KEYWORD", "10"))
DISTANCE_MAX = float(os.environ.get("PERSONA_RECALL_DISTANCE_MAX", "0.6"))
FINAL_TOP_K = int(os.environ.get("PERSONA_RECALL_FINAL_TOP_K", "8"))


@dataclass
class RecalledFact:
    fact_id: int
    category: str
    key: str
    value: str
    importance: int
    access_count: int
    distance: float
    score: float


def _parse_jst(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).replace(tzinfo=JST)
    except ValueError:
        return None


def _score(distance: float, age_days: float, importance: int, access_count: int) -> float:
    """重み式 (§5.4): relevance × recency × importance × access_factor

    access_factor は [0.5, 1.0] の範囲。新規 fact (access_count=0) でも
    0.5 のベースラインで残るようにし、cold-start 状態の fact が永久に
    沈黙する catch-22 を回避する (= recall でヒットしないと access_count
    が増えず、access_count が増えないと score=0 で recall に出ない問題)。
    """
    relevance = 1.0 / (1.0 + max(0.0, distance))
    recency = math.exp(-RECENCY_LAMBDA * max(0.0, age_days))
    imp = importance / 9.0
    acc_log = min(1.0, math.log(1 + access_count) / 5.0)
    acc = 0.5 + 0.5 * acc_log
    return relevance * recency * imp * acc


def _search_one(
    conn: sqlite3.Connection,
    embedding: list[float],
    k: int = TOP_K_PER_KEYWORD,
    distance_max: float = DISTANCE_MAX,
) -> list[RecalledFact]:
    """recall 検索の本体.

    boot 層 (persona/rule) は SessionStart で全件注入済みなので、recall
    では除外する。再注入はノイズ + token 浪費 (§8 / §10.1).
    boot 層が更新された時だけ on_user_prompt が dirty フラグ経由で
    additionalContext に prepend する仕組み (§8.1).
    """
    blob = pack(embedding)
    rows = conn.execute(
        """
        WITH knn AS (
          SELECT fact_id, distance
          FROM fact_embeddings
          WHERE embedding MATCH ? AND k = ?
        )
        SELECT f.id, f.category, f.key, f.value, f.importance, f.access_count,
               COALESCE(f.last_accessed_at, f.updated_at) AS ts, knn.distance
        FROM knn
        JOIN facts f ON f.id = knn.fact_id
        WHERE f.status = 'active'
          AND f.category NOT IN ('persona', 'rule')
        ORDER BY knn.distance
        """,
        (blob, k),
    ).fetchall()

    now = datetime.now(JST)
    out: list[RecalledFact] = []
    for r in rows:
        fid, cat, key, val, imp, acc, ts, dist = r
        if dist > distance_max:
            continue
        ts_dt = _parse_jst(ts)
        # 補正: tz-naive で保存される場合があるので tzinfo 補完。
        if ts_dt and ts_dt.tzinfo is None:
            ts_dt = ts_dt.replace(tzinfo=JST)
        age_days = (now - ts_dt).total_seconds() / 86400.0 if ts_dt else 0.0
        out.append(RecalledFact(
            fact_id=fid, category=cat, key=key, value=val,
            importance=imp, access_count=acc, distance=dist,
            score=_score(dist, age_days, imp, acc),
        ))
    return out


def search(
    conn: sqlite3.Connection,
    keyword_embeddings: list[list[float]],
    final_top_k: int = FINAL_TOP_K,
) -> list[RecalledFact]:
    """複数キーワードで検索 → 重複除去 (max score) → score 降順 top K."""
    if not keyword_embeddings:
        return []
    by_id: dict[int, RecalledFact] = {}
    for emb in keyword_embeddings:
        if not emb:
            continue
        for hit in _search_one(conn, emb):
            cur = by_id.get(hit.fact_id)
            if cur is None or hit.score > cur.score:
                by_id[hit.fact_id] = hit
    ranked = sorted(by_id.values(), key=lambda x: x.score, reverse=True)
    return ranked[:final_top_k]


def bump_access_counts(conn: sqlite3.Connection, fact_ids: list[int]) -> None:
    """recall でヒットした fact の access_count + last_accessed_at を更新."""
    if not fact_ids:
        return
    placeholders = ",".join("?" * len(fact_ids))
    conn.execute(
        f"UPDATE facts SET "
        f"  access_count = access_count + 1, "
        f"  last_accessed_at = datetime('now', '+9 hours') "
        f"WHERE id IN ({placeholders})",
        fact_ids,
    )
    conn.commit()
