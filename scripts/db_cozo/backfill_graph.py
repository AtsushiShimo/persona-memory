"""Cozo DB の episode 群から discussion_node + discussion_edge を遡及生成.

設計:
- episode を id 昇順で順次処理. 各 episode に対し:
  1. graph_extract.extract_node_with_relation で LLM 抽出
  2. node 候補があれば add_node + (任意) embedding
  3. prev_relation が非 null かつ同 topic の直前 node があれば add_edge
- topic_id が NULL の episode は session_id を topic_id とみなす (= 旧データ救済)
- 短文 user / 雑談 episode は事前 skip して LLM コール削減

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
from scripts.db_cozo.discussion import add_edge, add_node, get_last_node_in_topic
from scripts.db_cozo.repo import (
    ensure_topic, fetch_buffer, fetch_episode,
)
from scripts.db_cozo.graph_extract import (
    GRAPH_MODEL, extract_node_with_relation,
)
from scripts.shared.embedding import truncate_for_embedding
from scripts.shared.ollama import LLMClient, OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
SHORT_USER_SKIP_CHARS = int(os.environ.get("PERSONA_BACKFILL_USER_SKIP_CHARS", "50"))


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
    """1 episode を抽出 → node + edge を保存. 戻り値: (node_id, edge_kind)."""
    if _is_likely_empty(episode["role"], episode["content"]):
        return None, None
    topic_id = episode.get("topic_id") or episode["session_id"]
    if not episode.get("topic_id"):
        # 救済: episode に topic_id 紐付ける
        ensure_topic(client, topic_id)
        if not dry_run:
            client.run(
                "?[id, role, content, session_id, topic_id, timestamp] := "
                "*episode{id, role, content, session_id, timestamp}, "
                "id = $eid, topic_id = $tid "
                ":put episode {id => role, content, session_id, topic_id, timestamp}",
                {"eid": episode["id"], "tid": topic_id},
            )
    buf = fetch_buffer(client, episode["id"], 5, session_id=episode["session_id"])
    # 直前 node を LLM の手がかりとして渡す + 後で edge 接続元に再利用.
    prev = get_last_node_in_topic(client, topic_id)
    nc = extract_node_with_relation(
        episode["role"], episode["content"], buf, llm, prev_node=prev,
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
