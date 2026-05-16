"""Cozo DB の episode 群から discussion_node + discussion_edge を遡及生成.

0.7.3 改: 「生きてる話題箱」 方式の topic 同定 (identify_topic) を経由し、
backfill 中も新方式で topic を統合・割り当てる. リアルタイム経路と同等の
グラフ品質を過去ログにも適用するための救済テスト.

設計:
- episode を id 昇順で順次処理. 各 episode に対し:
  0. identify_topic で topic_id を再判定 (新方式 = embedding + LLM 2 段階)
     - alive_hours は大きく (デフォルト 8760h = 1 年) して全 topic を candidate に
     - 結果に応じて episode.topic_id を上書き
  1. 同 topic の最近ノードを候補として graph_extract に渡す
  2. node 候補があれば add_node + (任意) embedding
  3. prev_relation / target_id 両方の edge を張る
- 短文 user / 雑談 episode は事前 skip して LLM コール削減

env:
- PERSONA_BACKFILL_ALIVE_HOURS (default 8760 = 1 年): backfill 中の alive 判定窓
- PERSONA_BACKFILL_USER_SKIP_CHARS (default 50): 短い user 発話 skip 閾値

実行例:
  python -m scripts.db_cozo.backfill_graph --db <persona>.cozo.db
  python -m scripts.db_cozo.backfill_graph --db ... --limit 50 --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from pycozo.client import Client

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.discussion import (
    add_edge, add_node, get_last_node_in_topic, get_recent_nodes_in_topic,
)
from scripts.db_cozo.repo import (
    ensure_topic, fetch_buffer, fetch_episode,
)
from scripts.db_cozo.graph_extract import (
    GRAPH_MODEL, extract_node_with_relation,
)
from scripts.db_cozo.topic_identify import identify_topic
from scripts.shared.embedding import truncate_for_embedding
from scripts.shared.ollama import LLMClient, OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
SHORT_USER_SKIP_CHARS = int(os.environ.get("PERSONA_BACKFILL_USER_SKIP_CHARS", "50"))
# backfill 中は時系列が圧縮されるため alive 窓を大きく取る (実時間 1 年).
BACKFILL_ALIVE_HOURS = int(os.environ.get("PERSONA_BACKFILL_ALIVE_HOURS", "8760"))


def _is_likely_empty(role: str, content: str | None) -> bool:
    if not content or not content.strip():
        return True
    if role == "user" and len(content.strip()) < SHORT_USER_SKIP_CHARS:
        return True
    return False


def _episodes_pending(
    client: Client, since_episode_id: int | None = None,
) -> list[dict]:
    """既に discussion_node が紐付いている episode は skip.

    since_episode_id: 指定すると id >= 該当値だけ.
    """
    if since_episode_id is None:
        res = client.run("""
?[id, role, content, session_id, topic_id] :=
    *episode{id, role, content, session_id, topic_id},
    not had_node[id]
had_node[ep] := *discussion_node{episode_id: ep}
:order id
        """)
    else:
        res = client.run("""
?[id, role, content, session_id, topic_id] :=
    *episode{id, role, content, session_id, topic_id},
    id >= $sid,
    not had_node[id]
had_node[ep] := *discussion_node{episode_id: ep}
:order id
        """, {"sid": since_episode_id})
    return [
        {"id": r[0], "role": r[1], "content": r[2],
         "session_id": r[3], "topic_id": r[4]}
        for r in res.get("rows", [])
    ]


def backfill_one(
    client: Client, episode: dict, llm: LLMClient,
    embed_model: str = EMBED_MODEL,
    dry_run: bool = False,
) -> tuple[int | None, str | None]:
    """1 episode を抽出 → node + edge を保存. 戻り値: (node_id, edge_kind).

    0.7.3 改: identify_topic を呼んで「生きてる話題箱」 方式で topic を再判定.
    結果に応じて episode.topic_id を上書きする (= 旧 session_id ベースの
    粗い cluster を新方式で再構成).
    """
    if _is_likely_empty(episode["role"], episode["content"]):
        return None, None
    sid = episode["session_id"]
    if dry_run:
        topic_id = episode.get("topic_id") or sid
        ensure_topic(client, topic_id)
    else:
        # identify_topic で新方式の topic を確定 (alive 窓を大きく).
        try:
            result = identify_topic(
                client, role=episode["role"], content=episode["content"],
                session_id=sid, llm=llm, alive_hours=BACKFILL_ALIVE_HOURS,
            )
            topic_id = result.topic_id
        except Exception as e:
            sys.stderr.write(
                f"[graph-backfill] identify_topic failed ep#{episode['id']}: {e}\n"
            )
            topic_id = episode.get("topic_id") or sid
            ensure_topic(client, topic_id)
        # episode の topic_id を新 topic で上書き (= 旧 cluster の再構成)
        client.run(
            "?[id, role, content, session_id, topic_id, timestamp] := "
            "*episode{id, role, content, session_id, timestamp}, "
            "id = $eid, topic_id = $tid "
            ":put episode {id => role, content, session_id, topic_id, timestamp}",
            {"eid": episode["id"], "tid": topic_id},
        )

    buf = fetch_buffer(client, episode["id"], 5, session_id=sid)
    prev = get_last_node_in_topic(client, topic_id)
    # 0.7.3: 同 topic 内の最近ノードを候補として LLM に渡す (離れた edge 用).
    recent_nodes = get_recent_nodes_in_topic(client, topic_id, limit=10)
    if prev:
        recent_nodes = [n for n in recent_nodes if n["id"] != prev["id"]]
    nc = extract_node_with_relation(
        episode["role"], episode["content"], buf, llm,
        prev_node=prev, candidate_nodes=recent_nodes,
    )
    if nc is None:
        return None, None
    if dry_run:
        return -1, nc.prev_relation

    # embedding (title + content)
    emb_text = nc.title
    if nc.content:
        emb_text = f"{nc.title}\n{nc.content}"
    emb_text = truncate_for_embedding(emb_text)
    embedding: list[float] = []
    try:
        embedding = llm.embed(embed_model, emb_text) or []
    except Exception:
        embedding = []
    nid = add_node(
        client, kind=nc.kind, title=nc.title, state=nc.state,
        content=nc.content, episode_id=episode["id"],
        embedding=embedding if embedding else None,
    )
    edge_kind = None
    if nc.prev_relation and prev and prev["id"] != nid:
        add_edge(client, prev["id"], nid, nc.prev_relation)
        edge_kind = nc.prev_relation
    # 0.7.3: 離れたノードへの edge も同様に処理
    if nc.target_id and nc.target_relation and nc.target_id != nid:
        valid_ids = {n["id"] for n in recent_nodes}
        if prev:
            valid_ids.add(prev["id"])
        if nc.target_id in valid_ids:
            try:
                add_edge(client, nc.target_id, nid, nc.target_relation)
            except Exception as e:
                sys.stderr.write(
                    f"[graph-backfill] target edge add failed ep#{episode['id']}: {e}\n"
                )
    return nid, edge_kind


def backfill(
    db_path: Path, limit: int | None = None, dry_run: bool = False,
    progress: bool = True, llm: LLMClient | None = None,
    since_episode_id: int | None = None,
) -> dict:
    client = init_db(db_path)
    cli = llm or OllamaClient()
    eps = _episodes_pending(client, since_episode_id=since_episode_id)
    if limit is not None:
        eps = eps[:limit]
    total = len(eps)
    n_nodes = 0
    n_edges = 0
    interrupted = 0
    for i, ep in enumerate(eps, start=1):
        if progress:
            sys.stderr.write(
                f"[graph-backfill] {i}/{total} ep#{ep['id']} {ep['role']}\n"
            )
            sys.stderr.flush()
        try:
            nid, ek = backfill_one(client, ep, cli, dry_run=dry_run)
        except KeyboardInterrupt:
            interrupted = 1
            sys.stderr.write(f"[graph-backfill] interrupted at {i}/{total}\n")
            break
        if nid is not None:
            n_nodes += 1
        if ek is not None:
            n_edges += 1
    return {
        "episodes_seen": min(total, i if eps else 0),
        "nodes_added": n_nodes,
        "edges_added": n_edges,
        "interrupted": interrupted,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backfill discussion graph in Cozo DB")
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--since-episode-id", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    args = p.parse_args(argv)
    if not args.db.exists():
        sys.stderr.write(f"db not found: {args.db}\n")
        return 1
    out = backfill(
        args.db, limit=args.limit, dry_run=args.dry_run,
        progress=not args.no_progress,
        since_episode_id=args.since_episode_id,
    )
    print(f"  episodes_seen: {out['episodes_seen']}")
    print(f"  nodes_added: {out['nodes_added']}")
    print(f"  edges_added: {out['edges_added']}")
    print(f"  interrupted: {out['interrupted']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
