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
TOP_K_PER_KEYWORD = int(os.environ.get("PERSONA_RECALL_TOP_K_PER_KEYWORD", "25"))
# cosine 距離スケール [0, 2]:
#   0.0 = 完全一致 / 0.3 = 強い類似 / 0.5 = 弱い類似 / 1.0 = 直交 / 2.0 = 真逆
# nomic-embed-text の日本語短文は distance が 0.35-0.55 の狭帯域に圧縮される
# ため、関連 fact を漏らさないよう threshold は緩めに取り、最終的な関連性
# 判定は summarize_recall LLM (役割: librarian) に委ねる。
DISTANCE_MAX = float(os.environ.get("PERSONA_RECALL_DISTANCE_MAX", "0.6"))
FINAL_TOP_K = int(os.environ.get("PERSONA_RECALL_FINAL_TOP_K", "15"))


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
    # lint で auto_resolve された場合、撤回済の旧 value をここに添える.
    # 「以前は X と言っていたが撤回済み」 という補足を summarize LLM に渡し,
    # main エージェントの context に「両方提示+正解添え」 で流す.
    # source='lint_conflict' の supersede 時のみセットされる. None なら無し.
    retracted_value: str | None = None


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
    # supersedes が lint_conflict 由来なら旧 value を retracted_value として
    # 添える (両方提示+正解添え 設計). conversation 由来の通常 supersede は
    # ユーザーが日常的に書き換えてるだけなので補足不要 (= NULL).
    #
    # boot 層 (persona/rule) は SessionStart 全件注入なので recall では除外する.
    # ただし persona/playbook_* (= 状況依存ノウハウ) は SessionStart 注入から
    # 外して dynamic recall でのみ hit させる設計のため、 ここでは含める.
    rows = conn.execute(
        """
        WITH knn AS (
          SELECT fact_id, distance
          FROM fact_embeddings
          WHERE embedding MATCH ? AND k = ?
        )
        SELECT f.id, f.category, f.key, f.value, f.importance, f.access_count,
               COALESCE(f.last_accessed_at, f.updated_at) AS ts, knn.distance,
               old.value AS retracted_value
        FROM knn
        JOIN facts f ON f.id = knn.fact_id
        LEFT JOIN facts old
          ON old.id = f.supersedes
          AND old.source = 'lint_conflict'
        WHERE f.status = 'active'
          AND (
            f.category NOT IN ('persona', 'rule')
            OR f.key LIKE 'playbook_%'
          )
        ORDER BY knn.distance
        """,
        (blob, k),
    ).fetchall()

    now = datetime.now(JST)
    out: list[RecalledFact] = []
    for r in rows:
        fid, cat, key, val, imp, acc, ts, dist, retracted = r
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
            retracted_value=retracted,
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


# ── episodes 検索 (会話履歴を辿る用) ──────────────────────────────────────────

EPISODE_LIKE_LIMIT = int(os.environ.get("PERSONA_RECALL_EPISODE_LIMIT", "30"))
EPISODE_CONTENT_PREVIEW = int(os.environ.get("PERSONA_RECALL_EPISODE_PREVIEW", "300"))

# adaptive 設計:
# - HARD_MAX は『明らかに無関係』 を切るだけの広めの距離 (= safety net)
# - 各キーワードで PULL_K まで拾う (top-K nearest)
# - 重複除去後、最終 TARGET_HITS 件だけ採用
# - DB が rich になるほど『TARGET_HITS 番目の距離』 が自然と厳しくなる (= 動的)
# - DB が sparse なら無理に広げず、HARD_MAX 内のものだけ返す
EPISODE_HARD_DISTANCE_MAX = float(os.environ.get("PERSONA_RECALL_EPISODE_HARD_MAX", "0.7"))
EPISODE_TARGET_HITS = int(os.environ.get("PERSONA_RECALL_EPISODE_TARGET", "8"))
EPISODE_PULL_K = int(os.environ.get("PERSONA_RECALL_EPISODE_PULL", "30"))


@dataclass
class RecalledEpisode:
    episode_id: int
    role: str
    content: str  # 切り詰め済み
    timestamp: str


def search_episodes_by_keywords(
    conn: sqlite3.Connection,
    keywords: list[str],
    limit: int = EPISODE_LIKE_LIMIT,
) -> list[RecalledEpisode]:
    """SQL LIKE で episodes.content を全文部分一致検索 (back-compat 用).

    新コードは search_episodes_by_embeddings を使うこと。
    """
    if not keywords:
        return []
    cleaned = [k.strip() for k in keywords if k and k.strip()]
    if not cleaned:
        return []
    where = " OR ".join(["content LIKE ?"] * len(cleaned))
    params = [f"%{k}%" for k in cleaned] + [limit]
    rows = conn.execute(
        f"""
        SELECT MAX(id) AS id, role, content, MAX(timestamp) AS timestamp
        FROM episodes
        WHERE {where}
        GROUP BY content, role
        ORDER BY MAX(id) DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    out: list[RecalledEpisode] = []
    for r in rows:
        content = r[2] or ""
        if EPISODE_CONTENT_PREVIEW > 0 and len(content) > EPISODE_CONTENT_PREVIEW:
            content = content[:EPISODE_CONTENT_PREVIEW] + "…"
        out.append(RecalledEpisode(
            episode_id=r[0], role=r[1], content=content, timestamp=r[3],
        ))
    return out


def _search_episodes_one(
    conn: sqlite3.Connection,
    embedding: list[float],
    k: int = EPISODE_PULL_K,
    distance_max: float = EPISODE_HARD_DISTANCE_MAX,
) -> list[tuple[int, str, str, str, float]]:
    """1 keyword embedding で episodes をベクトル検索.

    重複 (content, role) は GROUP BY で潰し、各グループの最小距離を採用。
    """
    blob = pack(embedding)
    rows = conn.execute(
        """
        WITH knn AS (
          SELECT episode_id, distance
          FROM episode_embeddings
          WHERE embedding MATCH ? AND k = ?
        )
        SELECT MAX(e.id) AS id, e.role, e.content, MAX(e.timestamp) AS timestamp,
               MIN(knn.distance) AS distance
        FROM knn
        JOIN episodes e ON e.id = knn.episode_id
        GROUP BY e.content, e.role
        HAVING MIN(knn.distance) <= ?
        ORDER BY MIN(knn.distance)
        """,
        (blob, k, distance_max),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def search_episodes_by_embeddings(
    conn: sqlite3.Connection,
    embeddings: list[list[float]],
    target_hits: int = EPISODE_TARGET_HITS,
    hard_distance_max: float = EPISODE_HARD_DISTANCE_MAX,
    pull_k: int = EPISODE_PULL_K,
) -> list[RecalledEpisode]:
    """複数キーワード embedding で episodes を vec0 検索 (adaptive).

    アルゴリズム:
    1. 各 embedding につき pull_k 件を hard_distance_max 内で取得
    2. (content, role) で dedup、最小距離を採用
    3. 距離昇順で並べ、top target_hits だけ採用
       → DB が rich なら top target_hits 番目の距離が自然と厳しくなる (= 動的閾値)
       → DB が sparse なら hard_distance_max 内の全件、無理に広げない
    """
    if not embeddings:
        return []
    seen: dict[tuple[str, str], tuple[int, str, str, str, float]] = {}
    for emb in embeddings:
        if not emb:
            continue
        for hit in _search_episodes_one(conn, emb, k=pull_k, distance_max=hard_distance_max):
            key = (hit[1], hit[2])  # (role, content)
            cur = seen.get(key)
            if cur is None or hit[4] < cur[4]:
                seen[key] = hit
    ranked = sorted(seen.values(), key=lambda h: h[4])
    out: list[RecalledEpisode] = []
    for r in ranked[:target_hits]:
        content = r[2] or ""
        if EPISODE_CONTENT_PREVIEW > 0 and len(content) > EPISODE_CONTENT_PREVIEW:
            content = content[:EPISODE_CONTENT_PREVIEW] + "…"
        out.append(RecalledEpisode(
            episode_id=r[0], role=r[1], content=content, timestamp=r[3],
        ))
    return out
