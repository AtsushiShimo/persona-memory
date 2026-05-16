"""scripts/db/repo.py の Cozo 版.

旧 SQLite 経路と同じ関数シグネチャを提供して呼び出し側 (write/recall/hooks) の
移行コストを抑える. 戻り値も互換 (episode id は int).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pycozo.client import Client

from scripts.db_cozo.connection import next_id

JST = timezone(timedelta(hours=9))


def _now_ts() -> str:
    """旧 SQLite と同じ「JST naive datetime 文字列」 を返す."""
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def save_episode(
    client: Client,
    role: str,
    content: str,
    session_id: str,
    topic_id: str | None = None,
) -> int:
    """生発話を episode に保存し、 生成された id を返す."""
    # topic_id 解決 (PERSONA_TOPIC_DISABLE / continue_topic 互換)
    import os
    if topic_id is None and os.environ.get("PERSONA_TOPIC_DISABLE", "").strip() != "1":
        try:
            topic_id = get_active_topic(client, session_id)
            ensure_topic(client, topic_id)
        except Exception:
            topic_id = None

    eid = next_id(client, "episode")
    ts = _now_ts()
    if topic_id is not None:
        client.run(
            "?[id, role, content, session_id, topic_id, timestamp] <- "
            "[[$id, $role, $content, $sid, $tid, $ts]] "
            ":put episode {id => role, content, session_id, topic_id, timestamp}",
            {"id": eid, "role": role, "content": content, "sid": session_id,
             "tid": topic_id, "ts": ts},
        )
    else:
        client.run(
            "?[id, role, content, session_id, timestamp] <- "
            "[[$id, $role, $content, $sid, $ts]] "
            ":put episode {id => role, content, session_id, timestamp}",
            {"id": eid, "role": role, "content": content, "sid": session_id, "ts": ts},
        )
    return eid


def get_meta(client: Client, key: str) -> str | None:
    res = client.run(
        "?[v] := *meta{key: $k, value: v}",
        {"k": key},
    )
    rows = res.get("rows", [])
    return rows[0][0] if rows else None


def set_meta(client: Client, key: str, value: str) -> None:
    client.run(
        "?[key, value] <- [[$k, $v]] :put meta {key => value}",
        {"k": key, "v": value},
    )


# ── topic 状態管理 (旧 scripts.topic.state の Cozo 版) ────────────────────

def get_active_topic(client: Client, session_id: str) -> str:
    """継承指示が無ければ session_id をそのまま topic_id に."""
    res = client.run(
        "?[v] := *meta{key: $k, value: v}",
        {"k": f"topic_for_session_{session_id}"},
    )
    rows = res.get("rows", [])
    return rows[0][0] if rows else session_id


def set_active_topic(client: Client, session_id: str, topic_id: str) -> None:
    client.run(
        "?[key, value] <- [[$k, $v]] :put meta {key => value}",
        {"k": f"topic_for_session_{session_id}", "v": topic_id},
    )


def ensure_topic(client: Client, topic_id: str) -> None:
    """topic 行が無ければ作る. 既存なら last_active_at を更新."""
    res = client.run(
        "?[t] := *topic{id: $id, title: t}",
        {"id": topic_id},
    )
    ts = _now_ts()
    if res.get("rows"):
        client.run(
            "?[id, title, summary, created_at, last_active_at] := "
            "*topic{id, title, summary, created_at}, "
            "id = $id, "
            "last_active_at = $ts "
            ":put topic {id => title, summary, created_at, last_active_at}",
            {"id": topic_id, "ts": ts},
        )
    else:
        client.run(
            "?[id, created_at, last_active_at] <- "
            "[[$id, $ts, $ts]] "
            ":put topic {id => created_at, last_active_at}",
            {"id": topic_id, "ts": ts},
        )


# ── topic_summary_emb 操作 (0.7.6 「生きてる話題箱」 設計) ─────────────────

def upsert_topic_summary_emb(
    client: Client,
    topic_id: str,
    summary: str,
    embedding: list[float] | None,
) -> None:
    """topic ごとの summary 本文 + embedding を upsert.

    topic.summary (人間可読の表示用) も同時に更新する. 両者は同じ文字列を保持.
    """
    ts = _now_ts()
    if embedding and len(embedding) > 0:
        client.run(
            "?[topic_id, summary, embedding, updated_at] <- "
            "[[$tid, $sum, vec($emb), $ts]] "
            ":put topic_summary_emb {topic_id => summary, embedding, updated_at}",
            {"tid": topic_id, "sum": summary, "emb": embedding, "ts": ts},
        )
    else:
        client.run(
            "?[topic_id, summary, updated_at] <- [[$tid, $sum, $ts]] "
            ":put topic_summary_emb {topic_id => summary, updated_at}",
            {"tid": topic_id, "sum": summary, "ts": ts},
        )
    # topic.summary も同期更新 (人間可読表示・既存コード互換).
    res = client.run(
        "?[t, c] := *topic{id: $id, title: t, created_at: c}",
        {"id": topic_id},
    )
    rows = res.get("rows", [])
    if rows:
        title, created_at = rows[0]
        client.run(
            "?[id, title, summary, created_at, last_active_at] <- "
            "[[$id, $title, $sum, $cat, $ts]] "
            ":put topic {id => title, summary, created_at, last_active_at}",
            {"id": topic_id, "title": title, "sum": summary,
             "cat": created_at, "ts": ts},
        )
    else:
        # topic 行が無ければ作る (summary 付きで)
        client.run(
            "?[id, summary, created_at, last_active_at] <- "
            "[[$id, $sum, $ts, $ts]] "
            ":put topic {id => summary, created_at, last_active_at}",
            {"id": topic_id, "sum": summary, "ts": ts},
        )


def get_topic_summary_emb(client: Client, topic_id: str) -> dict | None:
    """topic_id の summary レコードを返す (summary, embedding, updated_at)."""
    res = client.run(
        "?[summary, embedding, updated_at] := "
        "*topic_summary_emb{topic_id: $tid, summary, embedding, updated_at}",
        {"tid": topic_id},
    )
    rows = res.get("rows", [])
    if not rows:
        return None
    r = rows[0]
    return {"summary": r[0], "embedding": r[1], "updated_at": r[2]}


def find_alive_topics_by_summary_emb(
    client: Client,
    embedding: list[float],
    alive_hours: int = 168,
    top_k: int = 5,
    distance_max: float = 0.5,
) -> list[dict]:
    """新発話 embedding に近い「生きてる」 topic を summary 経由で検索.

    alive_hours: topic.last_active_at が現在からこの時間内なら「生きてる」.
    distance_max: cosine 距離の閾値. これより近いものだけ返す.

    戻り値: [{"topic_id": str, "distance": float, "summary": str}, ...] (距離昇順).
    """
    if not embedding:
        return []
    cutoff = (datetime.now(JST) - timedelta(hours=alive_hours)).strftime("%Y-%m-%d %H:%M:%S")
    res = client.run(
        "?[dist, topic_id, summary] := "
        "~topic_summary_emb:vec_idx{topic_id, summary | "
        "query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
        "*topic{id: topic_id, last_active_at}, "
        "last_active_at >= $cutoff, "
        "dist < $dmax "
        ":order dist",
        {"q": embedding, "k": top_k, "cutoff": cutoff, "dmax": distance_max},
    )
    return [
        {"distance": r[0], "topic_id": r[1], "summary": r[2]}
        for r in res.get("rows", [])
    ]


def list_alive_topic_ids(client: Client, alive_hours: int = 168) -> list[str]:
    """生きてる topic の id 列を返す (last_active_at desc)."""
    cutoff = (datetime.now(JST) - timedelta(hours=alive_hours)).strftime("%Y-%m-%d %H:%M:%S")
    res = client.run(
        "?[id, last_active_at] := *topic{id, last_active_at}, "
        "last_active_at >= $cutoff "
        ":order -last_active_at",
        {"cutoff": cutoff},
    )
    return [r[0] for r in res.get("rows", [])]


def add_topic_relation(
    client: Client, from_topic_id: str, to_topic_id: str, kind: str = "派生",
) -> None:
    """topic 間のエッジを追加 (upsert). 同じ (from, to, kind) は重複扱い."""
    if not from_topic_id or not to_topic_id or from_topic_id == to_topic_id:
        return
    ts = _now_ts()
    client.run(
        "?[from_topic_id, to_topic_id, kind, ts] <- [[$f, $t, $k, $ts]] "
        ":put topic_relation {from_topic_id, to_topic_id, kind => ts}",
        {"f": from_topic_id, "t": to_topic_id, "k": kind, "ts": ts},
    )


def find_related_topics_by_summary_emb(
    client: Client,
    embedding: list[float],
    alive_hours: int = 168,
    top_k: int = 5,
    distance_min: float = 0.35,
    distance_max: float = 0.55,
    exclude_topic_id: str | None = None,
) -> list[dict]:
    """0.7.6: 新 topic と「関連はあるが同一ではない」 既存 topic を返す.

    識別閾値 (distance_min) より遠い = 同一話題ではない、 かつ
    関連閾値 (distance_max) より近い = 何らかの関連がある、 という帯域.
    マインドマップ的な topic_relation 自動生成に使う.
    """
    if not embedding:
        return []
    cutoff = (datetime.now(JST) - timedelta(hours=alive_hours)).strftime("%Y-%m-%d %H:%M:%S")
    res = client.run(
        "?[dist, topic_id, summary] := "
        "~topic_summary_emb:vec_idx{topic_id, summary | "
        "query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
        "*topic{id: topic_id, last_active_at}, "
        "last_active_at >= $cutoff, "
        "dist >= $dmin, dist < $dmax "
        ":order dist",
        {"q": embedding, "k": top_k, "cutoff": cutoff,
         "dmin": distance_min, "dmax": distance_max},
    )
    out = []
    for r in res.get("rows", []):
        tid = r[1]
        if exclude_topic_id and tid == exclude_topic_id:
            continue
        out.append({"distance": r[0], "topic_id": tid, "summary": r[2]})
    return out


def touch_topic(client: Client, topic_id: str) -> None:
    """topic.last_active_at を現在時刻に更新 (= 「触った」 印)."""
    ts = _now_ts()
    res = client.run(
        "?[title, summary, created_at] := "
        "*topic{id: $id, title, summary, created_at}",
        {"id": topic_id},
    )
    rows = res.get("rows", [])
    if not rows:
        return
    title, summary, created_at = rows[0]
    client.run(
        "?[id, title, summary, created_at, last_active_at] <- "
        "[[$id, $title, $sum, $cat, $ts]] "
        ":put topic {id => title, summary, created_at, last_active_at}",
        {"id": topic_id, "title": title, "sum": summary,
         "cat": created_at, "ts": ts},
    )


# ── 検索系 (recall パイプライン用) ───────────────────────────────────────

def fetch_episode(client: Client, episode_id: int) -> dict | None:
    res = client.run(
        "?[id, role, content, session_id, topic_id] := "
        "*episode{id, role, content, session_id, topic_id}, id = $id",
        {"id": episode_id},
    )
    rows = res.get("rows", [])
    if not rows:
        return None
    r = rows[0]
    return {
        "id": r[0], "role": r[1], "content": r[2],
        "session_id": r[3], "topic_id": r[4],
    }


def fetch_buffer(
    client: Client, before_id: int, n: int, session_id: str | None = None,
) -> list[dict]:
    """直前 N 発話 (before_id 未満) を昇順で返す.

    session_id 指定時は同 session に絞る (並行 session の混入防止).
    """
    if session_id is None:
        res = client.run(
            "?[id, role, content] := *episode{id, role, content}, id < $bid "
            ":order -id :limit $n",
            {"bid": before_id, "n": n},
        )
    else:
        res = client.run(
            "?[id, role, content] := *episode{id, role, content, session_id: $sid}, "
            "id < $bid :order -id :limit $n",
            {"bid": before_id, "n": n, "sid": session_id},
        )
    out = [{"id": r[0], "role": r[1], "content": r[2]} for r in res.get("rows", [])]
    return list(reversed(out))


def search_facts_vec(
    client: Client, embedding: list[float], top_k: int = 10,
    distance_max: float = 0.6,
) -> list[dict]:
    """発話 embedding に近い active fact を返す."""
    res = client.run(
        "?[dist, id, category, key, value, importance] := "
        "~fact:vec_idx{id, category, key, value, importance | "
        "query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
        "*fact{id, status: 'active'}, dist < $dmax "
        ":order dist",
        {"q": embedding, "k": top_k, "dmax": distance_max},
    )
    out = []
    for r in res.get("rows", []):
        out.append({
            "distance": r[0], "id": r[1], "category": r[2],
            "key": r[3], "value": r[4], "importance": r[5],
        })
    return out


def search_episodes_vec(
    client: Client, embedding: list[float], top_k: int = 5,
    distance_max: float = 0.6,
) -> list[dict]:
    res = client.run(
        "?[dist, id, role, content, session_id] := "
        "~episode:vec_idx{id, role, content, session_id | "
        "query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
        "dist < $dmax "
        ":order dist",
        {"q": embedding, "k": top_k, "dmax": distance_max},
    )
    out = []
    for r in res.get("rows", []):
        out.append({
            "distance": r[0], "id": r[1], "role": r[2],
            "content": r[3], "session_id": r[4],
        })
    return out


def search_topic_tags_vec(
    client: Client, embedding: list[float], top_k: int = 12,
    distance_max: float = 0.6,
) -> list[dict]:
    res = client.run(
        "?[dist, id, topic_id, tag] := "
        "~topic_tag:vec_idx{id, topic_id, tag | "
        "query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
        "dist < $dmax "
        ":order dist",
        {"q": embedding, "k": top_k, "dmax": distance_max},
    )
    return [
        {"distance": r[0], "id": r[1], "topic_id": r[2], "tag": r[3]}
        for r in res.get("rows", [])
    ]


def fetch_topics(client: Client, topic_ids: list[str]) -> dict[str, dict]:
    if not topic_ids:
        return {}
    res = client.run(
        "?[id, title, summary, last_active_at] := "
        "*topic{id, title, summary, last_active_at}, id in $ids",
        {"ids": topic_ids},
    )
    return {
        r[0]: {"title": r[1], "summary": r[2], "last_active_at": r[3]}
        for r in res.get("rows", [])
    }


def fetch_topic_episodes(
    client: Client, topic_id: str, limit: int = 60,
) -> list[dict]:
    """topic に紐付く episodes を古い順 (id 昇順) で返す.

    SessionEnd の summarize や recall の「流れ再構築」 で使う.
    """
    res = client.run(
        "?[id, role, content] := *episode{id, role, content, topic_id: $tid} "
        ":order -id :limit $n",
        {"tid": topic_id, "n": limit},
    )
    rows = list(reversed(res.get("rows", [])))
    return [{"id": r[0], "role": r[1], "content": r[2]} for r in rows]


def fetch_recent_episodes_for_topic(
    client: Client, topic_id: str, last_n: int = 5,
) -> list[dict]:
    """topic 末尾の N 発話 (新→古) を返す. recall で「最後のやり取り」 を生再生する用."""
    res = client.run(
        "?[id, role, content] := *episode{id, role, content, topic_id: $tid} "
        ":order -id :limit $n",
        {"tid": topic_id, "n": last_n},
    )
    return [{"id": r[0], "role": r[1], "content": r[2]} for r in res.get("rows", [])]


def find_similar_topics_by_emb(
    client: Client, embedding: list[float],
    top_k: int = 5,
    exclude_topic_id: str | None = None,
    distance_max: float = 0.6,
    episode_pool: int = 20,
) -> list[dict]:
    """新発話 embedding に近い過去 topic 候補を集約して返す.

    Cross-Session Topic Merge の前段. 「session 跨ぎで同議題を merge」 用に,
    session 横断 vec 検索 → topic_id 集約 → hit 数で並べ替え.

    アルゴリズム:
    1. episode_pool 件まで episodes_vec で session 横断近傍を取得
    2. 各 episode の topic_id を引いてグループ化
    3. exclude_topic_id (= 現在 active な topic) を除外
    4. hit 数 desc, min_distance asc で並べ替え

    戻り値: [
      {"topic_id": str, "hit_count": int, "min_distance": float,
       "representative_episode_ids": [int, ...]}, ...
    ] (best first, max top_k).
    """
    eps = search_episodes_vec(
        client, embedding, top_k=episode_pool, distance_max=distance_max,
    )
    if not eps:
        return []
    ep_ids = [e["id"] for e in eps]
    res = client.run(
        "?[id, topic_id] := *episode{id, topic_id}, id in $ids",
        {"ids": ep_ids},
    )
    ep_to_topic = {r[0]: r[1] for r in res.get("rows", [])}
    by_topic: dict[str, dict] = {}
    for e in eps:
        tid = ep_to_topic.get(e["id"])
        if not tid:
            continue
        if exclude_topic_id and tid == exclude_topic_id:
            continue
        bucket = by_topic.setdefault(tid, {
            "topic_id": tid,
            "hit_count": 0,
            "min_distance": float("inf"),
            "representative_episode_ids": [],
        })
        bucket["hit_count"] += 1
        if e["distance"] < bucket["min_distance"]:
            bucket["min_distance"] = e["distance"]
        if len(bucket["representative_episode_ids"]) < 3:
            bucket["representative_episode_ids"].append(e["id"])
    candidates = sorted(
        by_topic.values(),
        key=lambda b: (-b["hit_count"], b["min_distance"]),
    )
    return candidates[:top_k]
