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
