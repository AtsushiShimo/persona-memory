"""既存 topic (summary が無いもの) に summary 後付け生成する救済 backfill.

0.7.3 「生きてる話題箱」 設計の導入で topic_summary_emb 経路ができたが、
旧データの topic 群には summary が null のままのものがある. それらに対し
紐付く episode を集約して LLM で summary を生成し、 照合対象に乗せる.

実行例:
  python -m scripts.db_cozo.backfill_topic_summary --db <persona>.cozo.db
  python -m scripts.db_cozo.backfill_topic_summary --db ... --limit 50
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pycozo.client import Client

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.repo import (
    get_topic_summary_emb, upsert_topic_summary_emb,
)
from scripts.db_cozo.topic_summary import (
    generate_initial_summary, SUMMARY_MAX_CHARS,
)
from scripts.shared.ollama import LLMClient, OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")


def topics_needing_summary(client: Client) -> list[str]:
    """summary が null か、 topic_summary_emb に entry が無い topic_id を返す."""
    res = client.run(
        "?[id] := *topic{id, summary}, is_null(summary) "
        ":order id"
    )
    rows = res.get("rows", [])
    cands = [r[0] for r in rows]
    # topic_summary_emb 側で未登録の topic も追加
    res2 = client.run(
        "?[id] := *topic{id}, not has_sum[id] "
        "has_sum[tid] := *topic_summary_emb{topic_id: tid} "
        ":order id"
    )
    for r in res2.get("rows", []):
        if r[0] not in cands:
            cands.append(r[0])
    return cands


def fetch_episodes_for_topic(
    client: Client, topic_id: str, limit: int = 5,
) -> list[dict]:
    """topic_id に紐付く episode の最初 N 件 (id 昇順)."""
    res = client.run(
        "?[id, role, content] := *episode{id, role, content, topic_id: $tid} "
        ":order id :limit $lim",
        {"tid": topic_id, "lim": limit},
    )
    return [
        {"id": r[0], "role": r[1], "content": r[2]}
        for r in res.get("rows", [])
    ]


def synthesize_summary(
    episodes: list[dict], llm: LLMClient,
    max_chars: int = SUMMARY_MAX_CHARS,
) -> str:
    """複数 episode を 1 つの仮想発話に統合してから summary 生成."""
    if not episodes:
        return ""
    # 最初の episode を主軸に、 残りを buffer 風に追記して 1 件で抽出させる.
    combined = "\n".join(
        f"[{e['role']}] {(e['content'] or '').strip()[:300]}"
        for e in episodes
    )
    # generate_initial_summary は単発発話用. content に集約しても動く.
    return generate_initial_summary(
        role="mixed", content=combined, llm=llm, max_chars=max_chars,
    )


def backfill_one(
    client: Client, topic_id: str, llm: LLMClient, dry_run: bool = False,
) -> str | None:
    eps = fetch_episodes_for_topic(client, topic_id, limit=5)
    if not eps:
        return None
    summary = synthesize_summary(eps, llm)
    if not summary:
        return None
    if dry_run:
        return summary
    try:
        emb = llm.embed(EMBED_MODEL, summary) or []
    except Exception:
        emb = []
    upsert_topic_summary_emb(client, topic_id, summary, emb)
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backfill topic summary + embedding")
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if not args.db.exists():
        sys.stderr.write(f"db not found: {args.db}\n")
        return 1
    client = init_db(args.db)
    llm = OllamaClient()
    targets = topics_needing_summary(client)
    if args.limit is not None:
        targets = targets[:args.limit]
    total = len(targets)
    print(f"[backfill-topic-summary] {total} topic(s) need summary")
    n_done = 0
    for i, tid in enumerate(targets, start=1):
        sys.stderr.write(f"[{i}/{total}] {tid} ... ")
        sys.stderr.flush()
        try:
            sm = backfill_one(client, tid, llm, dry_run=args.dry_run)
        except KeyboardInterrupt:
            sys.stderr.write("interrupted\n")
            break
        except Exception as e:
            sys.stderr.write(f"FAIL ({e})\n")
            continue
        if sm:
            n_done += 1
            sys.stderr.write(f"OK ({sm[:50]}...)\n")
        else:
            sys.stderr.write("skipped (no episodes)\n")
    print(f"done: {n_done}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
