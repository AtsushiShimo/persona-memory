"""recall_triggers (想起トリガー学習) からランキング boost を計算 (0.6.15 Phase B).

Phase A (0.6.12) で蓄積した (query_embedding, hit_fact_ids, hit_episode_ids) を
活用し、 現 query に類似する過去 trigger の hit を boost する。 ユーザー個別の
再参照成功パターンが時間とともに recall ランキングに反映される (agentmemory の
generic RRF をパーソナライズで超える).

「忘れない」 原則: 検索ランキングへの寄与のみで履歴は消さない。
"""
from __future__ import annotations

import json
import os
import sqlite3

from scripts.shared.embedding import pack

# trigger 検索の上限 (近い順)
TRIGGER_TOP_K = int(os.environ.get("PERSONA_RECALL_TRIGGER_TOP_K", "10"))
# 採用する distance しきい値 (cosine distance, 0.0=同一, 2.0=反対)
TRIGGER_DISTANCE_MAX = float(os.environ.get("PERSONA_RECALL_TRIGGER_DISTANCE_MAX", "0.4"))
# boost gain (= 各 trigger 寄与の倍率). _score の典型値 (0-1 帯) と
# 釣り合うよう保守的に設定. 1 件 hit で score+0.3 程度.
TRIGGER_BOOST_GAIN = float(os.environ.get("PERSONA_RECALL_TRIGGER_GAIN", "0.3"))


def trigger_boost_enabled() -> bool:
    """env で boost を on/off. default ON.

    テスト等で off にできるよう '0' / '' / 'false' で無効化.
    """
    v = os.environ.get("PERSONA_RECALL_TRIGGER_BOOST", "1").strip().lower()
    return v not in ("", "0", "false", "no", "off")


def fetch_similar_triggers(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    top_k: int = TRIGGER_TOP_K,
    distance_max: float = TRIGGER_DISTANCE_MAX,
) -> list[dict]:
    """過去 trigger から query 近傍 top_k 件を distance 昇順で返す.

    sqlite-vec の vec_distance_cosine で SQL 側計算. trigger 数が増えても
    全行 cosine 計算なのでスケール限界はあるが、 数千件規模なら問題なし.
    """
    if not query_embedding:
        return []
    blob = pack(query_embedding)
    rows = conn.execute(
        """
        SELECT id, trigger_phrase, hit_fact_ids, hit_episode_ids,
               vec_distance_cosine(query_embedding, ?) AS distance
          FROM recall_triggers
         WHERE query_embedding IS NOT NULL
         ORDER BY distance ASC
         LIMIT ?
        """,
        (blob, top_k),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        dist = float(r[4]) if r[4] is not None else 1.0
        if dist > distance_max:
            continue
        try:
            fact_ids = json.loads(r[2] or "[]")
            ep_ids = json.loads(r[3] or "[]")
        except (json.JSONDecodeError, TypeError):
            fact_ids, ep_ids = [], []
        out.append(
            {
                "id": r[0], "phrase": r[1], "distance": dist,
                "hit_fact_ids": [int(x) for x in fact_ids if isinstance(x, (int, float))],
                "hit_episode_ids": [int(x) for x in ep_ids if isinstance(x, (int, float))],
            }
        )
    return out


def compute_boost_maps(
    triggers: list[dict],
) -> tuple[dict[int, float], dict[int, float]]:
    """trigger 群から fact_id / episode_id ごとの boost weight を集計.

    weight = sum over triggers of 1/(1+distance). 同 fact が複数 trigger に
    登場すれば加算的に強く boost. 距離が近いほど寄与大.

    戻り値: (fact_id → weight, episode_id → weight)
    """
    fact_boost: dict[int, float] = {}
    ep_boost: dict[int, float] = {}
    for t in triggers:
        w = 1.0 / (1.0 + max(0.0, t["distance"]))
        for fid in t["hit_fact_ids"]:
            fact_boost[fid] = fact_boost.get(fid, 0.0) + w
        for eid in t["hit_episode_ids"]:
            ep_boost[eid] = ep_boost.get(eid, 0.0) + w
    return fact_boost, ep_boost


def apply_fact_boost(hits, fact_boost: dict[int, float], gain: float = TRIGGER_BOOST_GAIN):
    """RecalledFact のリストに boost を加算し score 降順に並び替える.

    in-place で score を更新し、 ソート済みの新リストを返す.
    """
    if not fact_boost or gain <= 0:
        return hits
    for h in hits:
        b = fact_boost.get(h.fact_id, 0.0)
        if b > 0:
            h.score = h.score + gain * b
    return sorted(hits, key=lambda h: h.score, reverse=True)


def apply_episode_boost(
    episodes_hits, ep_boost: dict[int, float], gain: float = TRIGGER_BOOST_GAIN,
):
    """episodes_hits に boost を加算し score 降順に並び替える.

    EpisodeHit は score 属性を持つ前提 (scripts/recall/search.py).
    """
    if not ep_boost or gain <= 0:
        return episodes_hits
    for h in episodes_hits:
        b = ep_boost.get(h.episode_id, 0.0)
        if b > 0 and hasattr(h, "score"):
            h.score = h.score + gain * b
    if all(hasattr(h, "score") for h in episodes_hits):
        return sorted(episodes_hits, key=lambda h: h.score, reverse=True)
    return episodes_hits
