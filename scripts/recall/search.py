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

from scripts.shared.embedding import pack, unpack

JST = timezone(timedelta(hours=9))
# 0.6.7: 半減期 14 日 → 9 日に短縮 (古い meta 議論 / 完了済 problem 認識を風化).
RECENCY_LAMBDA = float(os.environ.get("PERSONA_RECALL_RECENCY_LAMBDA", "0.08"))
TOP_K_PER_KEYWORD = int(os.environ.get("PERSONA_RECALL_TOP_K_PER_KEYWORD", "25"))
# cosine 距離スケール [0, 2]:
#   0.0 = 完全一致 / 0.3 = 強い類似 / 0.5 = 弱い類似 / 1.0 = 直交 / 2.0 = 真逆
# nomic-embed-text の日本語短文は distance が 0.35-0.55 の狭帯域に圧縮される
# ため、関連 fact を漏らさないよう threshold は緩めに取り、最終的な関連性
# 判定は summarize_recall LLM (役割: librarian) に委ねる。
DISTANCE_MAX = float(os.environ.get("PERSONA_RECALL_DISTANCE_MAX", "0.6"))
FINAL_TOP_K = int(os.environ.get("PERSONA_RECALL_FINAL_TOP_K", "15"))

# 0.6.7: hit 同士の embedding 距離が閾値以内なら同一クラスタとし, score 最高
# 1 件だけ残す近傍重複 dedup. ローカル LLM の prompt では「同主旨を重複 fact 化
# しない」 と書いてあるが守られないケースが頻出 (例: 「トークン消費激しい」
# 系の meta 議論が複数 fact に分裂). recall 側で物理的に collapse する.
#   distance スケール: 0.10 = ほぼ同一 / 0.15 = 強い近傍 / 0.20 = 緩い近傍
# 0.15 は cosine sim 0.85 相当. 厳しすぎず緩すぎずの中間値.
CLUSTER_DISTANCE = float(os.environ.get("PERSONA_RECALL_CLUSTER_DISTANCE", "0.15"))
CLUSTER_ENABLED = os.environ.get("PERSONA_RECALL_CLUSTER", "1").strip() not in ("", "0", "false")


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


def _fetch_embeddings(
    conn: sqlite3.Connection, fact_ids: list[int]
) -> dict[int, list[float]]:
    """fact_id → embedding の dict を返す. 取得失敗は dict に含めない."""
    if not fact_ids:
        return {}
    placeholders = ",".join("?" * len(fact_ids))
    out: dict[int, list[float]] = {}
    try:
        rows = conn.execute(
            f"SELECT fact_id, embedding FROM fact_embeddings WHERE fact_id IN ({placeholders})",
            fact_ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    for fid, blob in rows:
        if blob:
            try:
                out[fid] = unpack(blob)
            except Exception:
                continue
    return out


def _cluster_dedup(
    ranked: list[RecalledFact],
    embeddings: dict[int, list[float]],
    cluster_distance: float = CLUSTER_DISTANCE,
) -> list[RecalledFact]:
    """近傍重複を greedy で潰す. score 降順前提.

    各 hit に対し既に採用済みの代表 hit との cosine 距離を計算し,
    閾値以内のものが 1 つでもあれば skip. なければ採用.
    embedding が無い hit は無条件で残す (= 旧 fact の embedding 欠落でも
    recall 自体は動き続ける safety net).
    """
    if not ranked:
        return []
    kept: list[RecalledFact] = []
    kept_embs: list[list[float]] = []
    for hit in ranked:
        emb = embeddings.get(hit.fact_id)
        if not emb:
            kept.append(hit)
            continue
        absorbed = False
        for kemb in kept_embs:
            if _cosine_distance(emb, kemb) <= cluster_distance:
                absorbed = True
                break
        if absorbed:
            continue
        kept.append(hit)
        kept_embs.append(emb)
    return kept


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """cosine distance [0, 2]. 0 = 同一方向, 1 = 直交, 2 = 真逆."""
    if not a or not b or len(a) != len(b):
        return 1.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - (dot / (math.sqrt(na) * math.sqrt(nb)))


def search(
    conn: sqlite3.Connection,
    keyword_embeddings: list[list[float]],
    final_top_k: int = FINAL_TOP_K,
    cluster_enabled: bool = CLUSTER_ENABLED,
    cluster_distance: float = CLUSTER_DISTANCE,
) -> list[RecalledFact]:
    """複数キーワードで検索 → 重複除去 (max score) → score 降順 → 近傍重複
    クラスタ dedup → top K.

    クラスタ dedup (0.6.7): hit 同士の embedding 距離が cluster_distance
    以内なら同一クラスタとし, score 最高の 1 件だけ残す. これでローカル LLM
    の prompt で抑えきれない「同主旨の重複 fact」 (例: 「トークン消費激しい」
    系 meta 議論が 6 件並走) を物理的に 1 件に collapse する.
    """
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
    if cluster_enabled and len(ranked) > 1:
        embs = _fetch_embeddings(conn, [h.fact_id for h in ranked])
        ranked = _cluster_dedup(ranked, embs, cluster_distance)
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


def search_episodes_by_fts(
    conn: sqlite3.Connection,
    queries: list[str],
    limit: int = EPISODE_LIKE_LIMIT,
) -> list[RecalledEpisode]:
    """FTS5 (trigram + BM25) で episodes を全文検索 (0.6.0 phase3 で追加,
    0.6.2 で recency 補正を追加).

    vec0 cosine の弱点 (nomic-embed-text の弁別力限界: 短文 / 固有名詞 /
    typo に弱い) を補う補完経路. trigram tokenizer なので日本語の連続文
    にも単語境界不要で効く. `Renju` / `Reiju` のような固有名詞は確実に
    hit する.

    **2 経路 union + recency 優先 sort** (0.6.2):
    - 経路 A: BM25 ランク top N (= 古くても語彙的に最も合致したもの)
    - 経路 B: 同 MATCH 内で timestamp DESC top N (= 新しい関連 hit)
    両者 union, 重複 (content, role) dedup, 最終的に **timestamp DESC** で
    並べる. これで「renju」 のような頻出 keyword で古い議論が上位を埋めても,
    直近の議論が確実に top に残る (ソフィア事案: 17:20 デフォルトカテゴリ
    決定が 5/8 ドメイン議論に押し出される回帰を防ぐ).

    episodes_fts テーブルが無い古い DB では空配列を返す (= upgrade 未実行
    の DB でも recall 自体は動き続ける).
    """
    if not queries:
        return []
    # (content, role) で dedup. value: (id, role, content, ts, rank)
    seen: dict[tuple[str, str], tuple[int, str, str, str, float]] = {}
    # 各経路の取得件数. union 後に limit でカットするので余裕を持って取る.
    per_path_limit = max(limit, 8)
    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        fts_q = '"' + q.replace('"', '""') + '"'
        try:
            # 経路 A: BM25 関連性 top
            rows_bm25 = conn.execute(
                """
                SELECT e.id, e.role, e.content, e.timestamp,
                       bm25(episodes_fts) AS rank
                FROM episodes_fts
                JOIN episodes e ON e.id = episodes_fts.rowid
                WHERE episodes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_q, per_path_limit),
            ).fetchall()
            # 経路 B: 同じ MATCH 内で timestamp 降順 top (recency 補正)
            rows_recent = conn.execute(
                """
                SELECT e.id, e.role, e.content, e.timestamp,
                       bm25(episodes_fts) AS rank
                FROM episodes_fts
                JOIN episodes e ON e.id = episodes_fts.rowid
                WHERE episodes_fts MATCH ?
                ORDER BY e.timestamp DESC
                LIMIT ?
                """,
                (fts_q, per_path_limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        for r in rows_bm25 + rows_recent:
            eid, role, content, ts, rank = r
            k = (content, role)
            if k not in seen or rank < seen[k][4]:
                seen[k] = (eid, role, content, ts, rank)
    # 最終 sort: timestamp 降順 (= 直近関連 hit を確実に上位に).
    # 同 timestamp は BM25 rank 昇順 (= 関連性高い方を優先).
    ranked = sorted(
        seen.values(),
        key=lambda x: (x[3] or "", -x[4]),  # ts desc, rank asc (rank は小さいほど良)
        reverse=True,
    )
    out: list[RecalledEpisode] = []
    for eid, role, content, ts, _rank in ranked[:limit]:
        prev = content
        if EPISODE_CONTENT_PREVIEW > 0 and prev and len(prev) > EPISODE_CONTENT_PREVIEW:
            prev = prev[:EPISODE_CONTENT_PREVIEW] + "…"
        out.append(RecalledEpisode(
            episode_id=eid, role=role, content=prev, timestamp=ts,
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
