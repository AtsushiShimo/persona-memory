"""議論グラフ (discussion_node + discussion_edge) の Cozo 操作.

設計の核:
- 各 episode のうち「主張・提案・決定・観察・質問」 等を含むものを node 化
- 直前の同 topic 内 node からの edge を必ず張る (= 流れの保証)
- edge kind は LLM が判定: 「賛同 / 反論 / 派生 / 決定 / 次バトン / 観察」
- 案 1 (旧 SQLite 版) で edges が 0 件だったのは、 そもそも edge 抽出を実装
  していなかったから. ここで補う.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pycozo.client import Client

from scripts.db_cozo.connection import next_id
from scripts.shared.embedding import truncate_for_embedding

JST = timezone(timedelta(hours=9))


def _now_ts() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def add_node(
    client: Client,
    *,
    kind: str,
    title: str,
    state: str = "proposed",
    content: str | None = None,
    episode_id: int | None = None,
    embedding: list[float] | None = None,
) -> int:
    """新規 discussion_node を 1 件追加. 採番した id を返す."""
    nid = next_id(client, "discussion_node")
    ts = _now_ts()
    if embedding and len(embedding) > 0:
        client.run(
            "?[id, ts, episode_id, kind, title, state, content, embedding] <- "
            "[[$id, $ts, $eid, $kind, $title, $state, $content, vec($emb)]] "
            ":put discussion_node {id => ts, episode_id, kind, title, state, "
            "content, embedding}",
            {"id": nid, "ts": ts, "eid": episode_id, "kind": kind,
             "title": title, "state": state, "content": content,
             "emb": embedding},
        )
    else:
        client.run(
            "?[id, ts, episode_id, kind, title, state, content] <- "
            "[[$id, $ts, $eid, $kind, $title, $state, $content]] "
            ":put discussion_node {id => ts, episode_id, kind, title, state, content}",
            {"id": nid, "ts": ts, "eid": episode_id, "kind": kind,
             "title": title, "state": state, "content": content},
        )
    return nid


def add_edge(
    client: Client, from_id: int, to_id: int, kind: str,
) -> None:
    """from_id → to_id の edge を kind 種別で追加 (重複は upsert)."""
    client.run(
        "?[from_id, to_id, kind, ts] <- [[$f, $t, $k, $ts]] "
        ":put discussion_edge {from_id, to_id, kind => ts}",
        {"f": from_id, "t": to_id, "k": kind, "ts": _now_ts()},
    )


def get_recent_nodes_in_topic(
    client: Client, topic_id: str, limit: int = 10,
) -> list[dict]:
    """同 topic_id に紐付く episode 経由の node を最新 N 件返す.

    0.7.6 「離れたノードへの意味的エッジ」 用. LLM に候補として渡し、
    target_id で参照させる.
    """
    if not topic_id:
        return []
    res = client.run(
        "?[id, kind, title, state, episode_id] := "
        "*discussion_node{id, kind, title, state, episode_id}, "
        "*episode{id: episode_id, topic_id: $tid} "
        ":order -id :limit $lim",
        {"tid": topic_id, "lim": limit},
    )
    rows = res.get("rows", [])
    return [
        {"id": r[0], "kind": r[1], "title": r[2],
         "state": r[3], "episode_id": r[4]}
        for r in rows
    ]


def get_last_node_in_topic(
    client: Client, topic_id: str,
) -> dict | None:
    """同 topic_id に紐付く episode を経由した node のうち、 最新の 1 件を返す.

    edges 接続元として使う. 「直前の議論ノード」 を特定するための手立て.
    """
    if not topic_id:
        return None
    res = client.run(
        "?[id, kind, title, state, episode_id] := "
        "*discussion_node{id, kind, title, state, episode_id}, "
        "*episode{id: episode_id, topic_id: $tid} "
        ":order -id :limit 1",
        {"tid": topic_id},
    )
    rows = res.get("rows", [])
    if not rows:
        return None
    r = rows[0]
    return {"id": r[0], "kind": r[1], "title": r[2],
            "state": r[3], "episode_id": r[4]}


def chain_from(
    client: Client, start_node_id: int, max_depth: int = 20,
) -> list[dict]:
    """start_node_id から outgoing edges を辿って到達できる node + edge を返す.

    戻り値は (node, edge_kind_to_next) のタプル列を dict 化したもの.
    末端 (out-edge 無し) の node まで含む.
    """
    res = client.run(
        """
reach[id, depth] := id = $start, depth = 0
reach[to_id, new_depth] :=
    *discussion_edge{from_id, to_id},
    reach[from_id, depth],
    depth < $max,
    new_depth = depth + 1
?[depth, id, kind, title, state] :=
    reach[id, depth],
    *discussion_node{id, kind, title, state}
        """,
        {"start": start_node_id, "max": max_depth},
    )
    rows = sorted(res.get("rows", []), key=lambda r: r[0])
    return [
        {"depth": r[0], "id": r[1], "kind": r[2], "title": r[3], "state": r[4]}
        for r in rows
    ]


def find_terminal_nodes(client: Client, candidate_ids: list[int]) -> list[dict]:
    """candidate_ids のうち outgoing edge を持たない (= 末端 = 議論の最後) node を返す."""
    if not candidate_ids:
        return []
    res = client.run(
        """
in_set[id] := id in $ids
has_out[from_id] := *discussion_edge{from_id}
?[id, kind, title, state] :=
    in_set[id],
    *discussion_node{id, kind, title, state},
    not has_out[id]
        """,
        {"ids": candidate_ids},
    )
    return [
        {"id": r[0], "kind": r[1], "title": r[2], "state": r[3]}
        for r in res.get("rows", [])
    ]


def nearest_nodes(
    client: Client, query_embedding: list[float],
    top_k: int = 5, distance_max: float = 0.6,
) -> list[dict]:
    """query embedding に近い discussion_node を vec0 で検索."""
    if not query_embedding:
        return []
    res = client.run(
        "?[dist, id, kind, title, state, episode_id] := "
        "~discussion_node:vec_idx{id, kind, title, state, episode_id | "
        "query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
        "dist < $dmax "
        ":order dist",
        {"q": query_embedding, "k": top_k, "dmax": distance_max},
    )
    return [
        {"distance": r[0], "id": r[1], "kind": r[2],
         "title": r[3], "state": r[4], "episode_id": r[5]}
        for r in res.get("rows", [])
    ]
