"""遡及抽出: 過去 episode から discussion_nodes を生成する script (0.6.18 Phase B).

過去に ingest 済みの episode は 0.6.17 以前の write LLM prompt がノード抽出を
含まなかったため、 discussion_nodes が空のまま. 本 script は同じ write LLM を
過去 episode に対して呼び直し、 nodes のみ抽出して discussion_nodes へ挿入する.

設計:
- **facts は一切変更しない**. nodes だけ抽出. master が懸念する prompt 副作用は
  原理的に発生しない (DB の facts table を触らないため).
- 同一 episode_id で既存 node がある場合は skip (idempotent).
- buffer は episode の直前 N 発話を fetch_buffer 経由で取得.
- LLM 失敗 episode は WARN 出して次へ.

実行例:
  python -m scripts.discussion.backfill --db ~/.persona-memory/<persona>.db
  python -m scripts.discussion.backfill --db ... --dry-run
  python -m scripts.discussion.backfill --db ... --since-episode-id 100
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from scripts.db.connection import connect
from scripts.discussion.graph import add_node
from scripts.shared.embedding import pack, truncate_for_embedding
from scripts.shared.ollama import LLMClient, OllamaClient
from scripts.write.extract import WRITE_MODEL, extract_facts_and_nodes
from scripts.write.run import EMBED_MODEL, fetch_buffer

DEFAULT_BUFFER_N = 3


def _episodes_to_process(
    conn: sqlite3.Connection, since_episode_id: int | None,
) -> list[dict]:
    """discussion_nodes が無い episode を昇順で返す.

    role='user' / 'assistant' どちらも対象. 既に nodes がある episode は skip.
    """
    sql = (
        "SELECT e.id, e.role, e.content, e.session_id "
        "FROM episodes e "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM discussion_nodes d WHERE d.episode_id = e.id"
        ") "
    )
    params: list = []
    if since_episode_id is not None:
        sql += "AND e.id >= ? "
        params.append(since_episode_id)
    sql += "ORDER BY e.id ASC"
    rows = conn.execute(sql, params).fetchall()
    return [
        {"id": r[0], "role": r[1], "content": r[2], "session_id": r[3]}
        for r in rows
    ]


def backfill_one(
    conn: sqlite3.Connection,
    episode: dict,
    client: LLMClient,
    buffer_n: int = DEFAULT_BUFFER_N,
    write_model: str = WRITE_MODEL,
    embed_model: str = EMBED_MODEL,
    dry_run: bool = False,
) -> int:
    """1 episode を遡及抽出. 戻り値 = 追加した node 件数."""
    eid = episode["id"]
    role = episode["role"]
    content = episode["content"]
    if not content or not content.strip():
        return 0
    buffer = fetch_buffer(
        conn, eid, buffer_n, session_id=episode.get("session_id"),
    )
    try:
        _facts, nodes = extract_facts_and_nodes(
            role, content, buffer, client, model=write_model,
        )
    except Exception as e:
        sys.stderr.write(f"[backfill] WARN episode_id={eid} extract failed: {e}\n")
        return 0

    if not nodes:
        return 0

    if dry_run:
        print(f"  episode_id={eid}: {len(nodes)} node(s) would be added")
        for nc in nodes:
            print(f"    - [{nc.kind}/{nc.state}] {nc.title}")
        return len(nodes)

    added = 0
    for nc in nodes:
        if not nc.is_valid():
            continue
        embed_text = nc.title
        if nc.content:
            embed_text = f"{nc.title}\n{nc.content}"
        embed_text = truncate_for_embedding(embed_text)
        emb_blob: bytes | None = None
        try:
            vec = client.embed(embed_model, embed_text)
            if vec:
                emb_blob = pack(vec)
        except Exception:
            emb_blob = None
        try:
            add_node(
                conn,
                kind=nc.kind, title=nc.title, state=nc.state,
                content=nc.content, episode_id=eid,
                embedding=emb_blob,
            )
            added += 1
        except Exception as e:
            sys.stderr.write(
                f"[backfill] WARN episode_id={eid} add_node failed: {e}\n"
            )
    return added


def backfill(
    db_path: Path,
    since_episode_id: int | None = None,
    buffer_n: int = DEFAULT_BUFFER_N,
    dry_run: bool = False,
    client: LLMClient | None = None,
) -> dict[str, int]:
    """戻り値: { episodes_seen, episodes_with_nodes, nodes_added }."""
    conn = connect(db_path)
    cli = client or OllamaClient()
    seen = 0
    with_nodes = 0
    added_total = 0
    try:
        eps = _episodes_to_process(conn, since_episode_id)
        for ep in eps:
            seen += 1
            n = backfill_one(conn, ep, cli, buffer_n=buffer_n, dry_run=dry_run)
            if n > 0:
                with_nodes += 1
                added_total += n
    finally:
        conn.close()
    return {
        "episodes_seen": seen,
        "episodes_with_nodes": with_nodes,
        "nodes_added": added_total,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill discussion_nodes for past episodes (0.6.18)",
    )
    parser.add_argument("--db", type=Path, required=True, help="Path to persona DB")
    parser.add_argument(
        "--since-episode-id", type=int, default=None,
        help="Process episodes with id >= this value (default: all unprocessed)",
    )
    parser.add_argument(
        "--buffer-n", type=int, default=DEFAULT_BUFFER_N,
        help=f"Number of preceding episodes to use as context (default {DEFAULT_BUFFER_N})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be extracted without inserting nodes",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    print(f"=== discussion_nodes backfill: {args.db} ===")
    if args.dry_run:
        print("(dry-run: no DB writes)")
    counts = backfill(
        args.db, since_episode_id=args.since_episode_id,
        buffer_n=args.buffer_n, dry_run=args.dry_run,
    )
    print(
        f"  episodes_seen={counts['episodes_seen']}, "
        f"episodes_with_nodes={counts['episodes_with_nodes']}, "
        f"nodes_added={counts['nodes_added']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
