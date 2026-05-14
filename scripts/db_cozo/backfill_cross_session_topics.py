"""旧 episode の topic_id を主題ベース cluster に振り直す (Cross-Session Topic Merge backfill).

設計:
- 対象は **topic_id == session_id** の episode のみ (= legacy fallback).
  sub- prefix や明示割り当て済の topic_id は触らない (safety net).
- 1 episode ずつ:
  1. 自身の embedding を使って find_similar_topics_by_emb で候補取得
  2. light LLM が「同議題か?」 を判定
  3. match なら topic_id を書き換え

これで「session_id を topic 代用していた旧データ」 を「主題ベース cluster」 に
統合できる. backfill_graph と組み合わせると edge も session 跨ぎで張れるようになる.

実行例:
  python -m scripts.db_cozo.backfill_cross_session_topics --db <persona>.cozo.db
  python -m scripts.db_cozo.backfill_cross_session_topics --db ... --dry-run
  python -m scripts.db_cozo.backfill_cross_session_topics --db ... --limit 50
  python -m scripts.db_cozo.backfill_cross_session_topics --db ... --count-only
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pycozo.client import Client

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.repo import (
    ensure_topic, fetch_recent_episodes_for_topic,
    find_similar_topics_by_emb,
)
from scripts.db_cozo.topic_shift import (
    MERGE_MODEL, MERGE_NUM_CTX,
    build_merge_prompt, parse_merge_judgment,
)
from scripts.shared.ollama import LLMClient, OllamaClient


def _legacy_episodes(client: Client) -> list[dict]:
    """topic_id == session_id (= legacy fallback) の episode のみ.

    embedding を持つもの (HNSW 検索可能) に限定.
    """
    res = client.run("""
?[id, role, content, session_id, topic_id, embedding] :=
    *episode{id, role, content, session_id, topic_id, embedding},
    topic_id = session_id,
    !is_null(embedding)
:order id
    """)
    return [
        {"id": r[0], "role": r[1], "content": r[2],
         "session_id": r[3], "topic_id": r[4], "embedding": r[5]}
        for r in res.get("rows", [])
    ]


def _count_legacy(client: Client) -> int:
    res = client.run("""
?[count(id)] :=
    *episode{id, session_id, topic_id, embedding},
    topic_id = session_id,
    !is_null(embedding)
    """)
    rows = res.get("rows", [])
    return rows[0][0] if rows else 0


def _update_episode_topic(client: Client, episode_id: int, new_topic_id: str) -> None:
    """既存 episode の topic_id を上書き. その他 column は維持."""
    client.run(
        "?[id, role, content, session_id, topic_id, timestamp, summary, embedding] := "
        "*episode{id, role, content, session_id, timestamp, summary, embedding}, "
        "id = $eid, topic_id = $tid "
        ":put episode {id => role, content, summary, session_id, topic_id, "
        "timestamp, embedding}",
        {"eid": episode_id, "tid": new_topic_id},
    )


def merge_one(
    client: Client, episode: dict, llm: LLMClient,
    merge_model: str = MERGE_MODEL,
) -> tuple[bool, str | None]:
    """1 episode の merge 判定 → 必要なら topic 書き換え.

    戻り値: (merged, new_topic_id or None).
    """
    emb = episode.get("embedding")
    if not emb:
        return False, None
    candidates = find_similar_topics_by_emb(
        client, emb, top_k=5,
        exclude_topic_id=episode["topic_id"],
    )
    if not candidates:
        return False, None
    for cand in candidates:
        tid = cand["topic_id"]
        past = fetch_recent_episodes_for_topic(client, tid, last_n=5)
        if not past:
            continue
        prompt = build_merge_prompt(past, episode["content"][:500])
        try:
            response = llm.generate(merge_model, prompt, num_ctx=MERGE_NUM_CTX)
        except Exception:
            continue
        is_match, _ = parse_merge_judgment(response)
        if is_match:
            ensure_topic(client, tid)
            _update_episode_topic(client, episode["id"], tid)
            return True, tid
    return False, None


def backfill(
    db_path: Path, limit: int | None = None, dry_run: bool = False,
    progress: bool = True, llm: LLMClient | None = None,
) -> dict:
    client = init_db(db_path)
    cli = llm or OllamaClient()
    eps = _legacy_episodes(client)
    if limit is not None:
        eps = eps[:limit]
    total = len(eps)
    n_merged = 0
    interrupted = 0
    i = 0
    for i, ep in enumerate(eps, start=1):
        if progress:
            sys.stderr.write(
                f"[topic-merge-backfill] {i}/{total} ep#{ep['id']}\n"
            )
            sys.stderr.flush()
        try:
            if dry_run:
                if ep.get("embedding"):
                    cand = find_similar_topics_by_emb(
                        client, ep["embedding"], top_k=1,
                        exclude_topic_id=ep["topic_id"],
                    )
                    if cand:
                        n_merged += 1
                continue
            merged, _ = merge_one(client, ep, cli)
            if merged:
                n_merged += 1
        except KeyboardInterrupt:
            interrupted = 1
            sys.stderr.write(
                f"[topic-merge-backfill] interrupted at {i}/{total}\n",
            )
            break
    return {
        "episodes_seen": min(total, i) if eps else 0,
        "merged": n_merged,
        "interrupted": interrupted,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Backfill cross-session topic merge in Cozo DB",
    )
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--count-only", action="store_true",
                   help="件数だけ stdout に出して終了")
    args = p.parse_args(argv)
    if not args.db.exists():
        sys.stderr.write(f"db not found: {args.db}\n")
        return 1
    if args.count_only:
        client = init_db(args.db)
        print(_count_legacy(client))
        return 0
    out = backfill(
        args.db, limit=args.limit, dry_run=args.dry_run,
        progress=not args.no_progress,
    )
    print(f"  episodes_seen: {out['episodes_seen']}")
    print(f"  merged: {out['merged']}")
    print(f"  interrupted: {out['interrupted']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
