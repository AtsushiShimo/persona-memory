"""遡及抽出: 過去 episode から topic_tags を生成する script (0.6.24).

過去に保存済みの episode は 0.6.24 以前 = topic 機能無し時代に書かれているため
topic_id が NULL で topic_tags も無い. 本 script はそれら episode に対して:
  1. topic_id を後付けで割り振る (デフォルト: session_id を流用. もしくは
     `--topic-id <fixed>` で全件 1 つの topic に集約)
  2. light LLM で tag を抽出し topic_tags + topic_tag_embeddings を構築

設計:
- **facts は一切変更しない**. tag_extract は別 prompt で動く.
- 既に topic_tags が紐付いている episode は skip (idempotent).
- LLM 失敗 episode は WARN を出して次へ.
- Ctrl+C で安全に中断可 (各 episode 単位で commit).
- 進捗を 1 episode ごとに stderr 表示.

実行例:
  python -m scripts.topic.backfill_tags --db ~/.persona-memory/<persona>.db
  python -m scripts.topic.backfill_tags --db ... --dry-run
  python -m scripts.topic.backfill_tags --db ... --limit 50
  python -m scripts.topic.backfill_tags --db ... --topic-id global  # 全件 1 topic
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from scripts.db.connection import connect
from scripts.shared.ollama import LLMClient, OllamaClient
from scripts.topic.persist import EMBED_MODEL_DEFAULT, save_tags
from scripts.topic.state import ensure_topic
from scripts.topic.tag_extract import TAG_MODEL, extract_tags
from scripts.write.run import fetch_buffer

DEFAULT_BUFFER_N = 3

# 旧 episodes の救済では light モデルで十分. PERSONA_TAG_MODEL を override.
DEFAULT_BACKFILL_MODEL = TAG_MODEL

# 短文 + user role の episode は tag 抽出余地が薄い (相槌・確認の類).
# discussion backfill と同じ閾値.
SHORT_USER_SKIP_CHARS = int(os.environ.get("PERSONA_BACKFILL_USER_SKIP_CHARS", "50"))


def _is_likely_empty_episode(role: str, content: str | None) -> bool:
    if not content or not content.strip():
        return True
    if role == "user" and len(content.strip()) < SHORT_USER_SKIP_CHARS:
        return True
    return False


def _episodes_to_process(
    conn: sqlite3.Connection,
    since_episode_id: int | None,
    *,
    apply_short_skip: bool = True,
    only_missing_topic_tags: bool = True,
) -> list[dict]:
    """tag が無い episode を昇順で返す.

    only_missing_topic_tags=True: 該当 episode の topic_id 配下に topic_tags が
    1 件も無いものを対象 (= 完全未処理). 既に部分的に tag が付いているものは
    skip して LLM コール削減.
    """
    if only_missing_topic_tags:
        sql = (
            "SELECT e.id, e.role, e.content, e.session_id, e.topic_id "
            "FROM episodes e "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM topic_tags t "
            "  WHERE t.topic_id = COALESCE(e.topic_id, e.session_id)"
            ") "
        )
    else:
        sql = (
            "SELECT id, role, content, session_id, topic_id FROM episodes WHERE 1=1 "
        )
    params: list = []
    if since_episode_id is not None:
        sql += "AND e.id >= ? " if only_missing_topic_tags else "AND id >= ? "
        params.append(since_episode_id)
    sql += "ORDER BY id ASC" if not only_missing_topic_tags else "ORDER BY e.id ASC"
    rows = conn.execute(sql, params).fetchall()
    out: list[dict] = []
    for r in rows:
        if apply_short_skip and _is_likely_empty_episode(r[1], r[2]):
            continue
        out.append({
            "id": r[0], "role": r[1], "content": r[2],
            "session_id": r[3], "topic_id": r[4],
        })
    return out


def _resolve_topic_id(
    conn: sqlite3.Connection,
    episode: dict,
    fixed_topic_id: str | None,
) -> str:
    """この episode に割り当てる topic_id を決定.

    優先順位:
      1. fixed_topic_id 引数 (--topic-id 指定時)
      2. episode.topic_id (既に紐付いている)
      3. episode.session_id (新規 topic として session_id を流用)
    """
    if fixed_topic_id:
        return fixed_topic_id
    if episode.get("topic_id"):
        return episode["topic_id"]
    return episode["session_id"]


def backfill_one(
    conn: sqlite3.Connection,
    episode: dict,
    client: LLMClient,
    buffer_n: int = DEFAULT_BUFFER_N,
    model: str = DEFAULT_BACKFILL_MODEL,
    embed_model: str = EMBED_MODEL_DEFAULT,
    fixed_topic_id: str | None = None,
    dry_run: bool = False,
) -> tuple[str, int]:
    """1 episode から tag を抽出 → topic_tags へ保存.

    戻り値: (resolved_topic_id, 追加した tag 件数).
    """
    eid = episode["id"]
    role = episode["role"]
    content = episode["content"]
    if not content or not content.strip():
        return ("", 0)
    topic_id = _resolve_topic_id(conn, episode, fixed_topic_id)
    buffer = fetch_buffer(conn, eid, buffer_n, session_id=episode.get("session_id"))
    try:
        tags = extract_tags(role, content, buffer, client, model=model)
    except Exception as e:
        sys.stderr.write(f"[topic-backfill] WARN episode_id={eid} extract failed: {e}\n")
        return (topic_id, 0)
    if not tags:
        return (topic_id, 0)

    if dry_run:
        print(f"  episode_id={eid}: {len(tags)} tag(s) → topic_id={topic_id}")
        for t in tags:
            print(f"    - {t}")
        return (topic_id, len(tags))

    # episode に topic_id がまだ無ければ後付けする
    if not episode.get("topic_id"):
        try:
            ensure_topic(conn, topic_id)
            conn.execute(
                "UPDATE episodes SET topic_id = ? WHERE id = ? AND topic_id IS NULL",
                (topic_id, eid),
            )
            conn.commit()
        except Exception as e:
            sys.stderr.write(
                f"[topic-backfill] WARN episode_id={eid} topic_id update failed: {e}\n"
            )
    saved = save_tags(conn, topic_id, tags, client, embed_model)
    return (topic_id, saved)


def count_pending_episodes(
    db_path: Path,
    since_episode_id: int | None = None,
    *,
    apply_short_skip: bool = True,
) -> int:
    """backfill 対象 episode 件数. 推定時間表示用 (LLM 呼ばない)."""
    conn = connect(db_path)
    try:
        return len(
            _episodes_to_process(
                conn, since_episode_id, apply_short_skip=apply_short_skip,
            )
        )
    finally:
        conn.close()


def backfill(
    db_path: Path,
    since_episode_id: int | None = None,
    buffer_n: int = DEFAULT_BUFFER_N,
    dry_run: bool = False,
    limit: int | None = None,
    client: LLMClient | None = None,
    progress: bool = False,
    model: str = DEFAULT_BACKFILL_MODEL,
    apply_short_skip: bool = True,
    fixed_topic_id: str | None = None,
) -> dict[str, int]:
    """戻り値: { episodes_seen, episodes_with_tags, tags_added, interrupted }."""
    conn = connect(db_path)
    cli = client or OllamaClient()
    seen = 0
    with_tags = 0
    added_total = 0
    interrupted = 0
    try:
        eps = _episodes_to_process(
            conn, since_episode_id, apply_short_skip=apply_short_skip,
        )
        if limit is not None:
            eps = eps[:limit]
        total = len(eps)
        for i, ep in enumerate(eps, start=1):
            if progress:
                sys.stderr.write(
                    f"[topic-backfill] {i}/{total} episode_id={ep['id']} role={ep['role']} ...\n"
                )
                sys.stderr.flush()
            try:
                _, n = backfill_one(
                    conn, ep, cli, buffer_n=buffer_n,
                    model=model, dry_run=dry_run,
                    fixed_topic_id=fixed_topic_id,
                )
            except KeyboardInterrupt:
                interrupted = 1
                sys.stderr.write(
                    f"[topic-backfill] interrupted at {i}/{total} "
                    f"(progress saved; re-run to continue)\n"
                )
                break
            seen += 1
            if n > 0:
                with_tags += 1
                added_total += n
                if progress:
                    sys.stderr.write(
                        f"[topic-backfill]   → +{n} tag(s) (cumulative {added_total})\n"
                    )
                    sys.stderr.flush()
    finally:
        conn.close()
    return {
        "episodes_seen": seen,
        "episodes_with_tags": with_tags,
        "tags_added": added_total,
        "interrupted": interrupted,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backfill topic_tags from past episodes")
    p.add_argument("--db", required=True, type=Path, help="persona DB path")
    p.add_argument("--since-episode-id", type=int, default=None)
    p.add_argument("--buffer-n", type=int, default=DEFAULT_BUFFER_N)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--no-short-skip", action="store_true",
                   help="短文 user 発話も LLM に投げる (default: skip)")
    p.add_argument("--topic-id", default=None,
                   help="指定するとその topic_id に全 episodes をまとめる")
    p.add_argument("--model", default=DEFAULT_BACKFILL_MODEL,
                   help=f"tag 抽出 LLM (default: {DEFAULT_BACKFILL_MODEL})")
    p.add_argument("--count-only", action="store_true",
                   help="件数だけ表示して終了 (LLM 呼ばない)")
    args = p.parse_args(argv)

    os.environ["PERSONA_MEMORY_DB"] = str(args.db)

    if args.count_only:
        n = count_pending_episodes(
            args.db, since_episode_id=args.since_episode_id,
            apply_short_skip=not args.no_short_skip,
        )
        print(f"pending episodes: {n}")
        return 0

    n = count_pending_episodes(
        args.db, since_episode_id=args.since_episode_id,
        apply_short_skip=not args.no_short_skip,
    )
    if args.limit is not None:
        n = min(n, args.limit)
    # 推定 1.5s/件 (light gemma3:4b warm) ベース
    est_min = (n * 1.5) / 60
    sys.stderr.write(
        f"[topic-backfill] target episodes: {n} "
        f"(estimated ~{est_min:.1f} min @ 1.5s/episode)\n"
    )

    result = backfill(
        args.db,
        since_episode_id=args.since_episode_id,
        buffer_n=args.buffer_n,
        dry_run=args.dry_run,
        limit=args.limit,
        progress=not args.no_progress,
        model=args.model,
        apply_short_skip=not args.no_short_skip,
        fixed_topic_id=args.topic_id,
    )
    print(
        f"done: episodes_seen={result['episodes_seen']}, "
        f"episodes_with_tags={result['episodes_with_tags']}, "
        f"tags_added={result['tags_added']}, "
        f"interrupted={result['interrupted']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
