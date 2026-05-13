"""遡及抽出: 過去 episode から discussion_nodes を生成する script (0.6.19).

過去に保存済みの episode は 0.6.17 以前の write LLM prompt がノード抽出を
含まなかったため、 discussion_nodes が空のまま. 本 script は同じ write LLM を
過去 episode に対して呼び直し、 nodes のみ抽出して discussion_nodes へ挿入する.

設計:
- **facts は一切変更しない**. nodes だけ抽出. master が懸念する prompt 副作用は
  原理的に発生しない (DB の facts table を触らないため).
- 同一 episode_id で既存 node がある場合は skip (idempotent).
- buffer は episode の直前 N 発話を fetch_buffer 経由で取得.
- LLM 失敗 episode は WARN 出して次へ.
- Ctrl+C で安全に中断可能 (各 add_node で即 commit するため途中まで保存される).
- 進捗を 1 episode ごとに stderr に表示.

実行例:
  python -m scripts.discussion.backfill --db ~/.persona-memory/<persona>.db
  python -m scripts.discussion.backfill --db ... --dry-run
  python -m scripts.discussion.backfill --db ... --since-episode-id 100
  python -m scripts.discussion.backfill --db ... --limit 20  # 部分実行
"""
from __future__ import annotations

import argparse
import os
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

# backfill 専用 default model:
# 通常の write は heavy (gemma3:12b) を使うが backfill は重すぎる.
# 構造抽出 (kind/title/state 程度) なら light (gemma3:4b) で十分捌けて
# 1 件 ~3-5s と heavy の数倍速い. 失敗率がやや上がる懸念はあるが
# **facts は触らない** 設計のため誤抽出が DB を汚染する経路が無い.
DEFAULT_BACKFILL_MODEL = os.environ.get(
    "PERSONA_LIGHT_MODEL",
    os.environ.get("PERSONA_JUDGE_MODEL", "gemma3:4b"),
)

# 短文 + user role の episode はノード抽出されない事が殆ど (= 「OK」「うん」「了解」
# 等の相槌・確認). LLM 呼ばずに skip して時間とトークンを節約する.
# assistant 短文 (= 短い回答) は decision/observation 抽出余地があるため除外しない.
SHORT_USER_SKIP_CHARS = int(os.environ.get("PERSONA_BACKFILL_USER_SKIP_CHARS", "50"))


def _is_likely_empty_episode(role: str, content: str | None) -> bool:
    """LLM を呼んでもまずノードが出ない episode かを軽量判定.

    短文かつ user の発話は相槌・確認 (「OK」「うん」「了解」「ありがとう」 等)
    である確率が高く、 議論ノードを抽出する余地が無い. これを事前に弾くことで
    backfill の LLM 呼び出し回数を大幅削減 (= 1993 件のうち半数以上が該当する
    プロジェクトもある).

    assistant の短文は短い結論や提案 (decision/observation 候補) の可能性が
    あるため除外しない.
    """
    if not content or not content.strip():
        return True
    if role == "user" and len(content.strip()) < SHORT_USER_SKIP_CHARS:
        return True
    return False


def _episodes_to_process(
    conn: sqlite3.Connection, since_episode_id: int | None,
    *, apply_short_skip: bool = True,
) -> list[dict]:
    """discussion_nodes が無い episode を昇順で返す.

    role='user' / 'assistant' どちらも対象. 既に nodes がある episode は skip.
    apply_short_skip=True (default) の時、 短文 user 発話は LLM 呼ばずに飛ばす.
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
    out: list[dict] = []
    for r in rows:
        if apply_short_skip and _is_likely_empty_episode(r[1], r[2]):
            continue
        out.append({"id": r[0], "role": r[1], "content": r[2], "session_id": r[3]})
    return out


def backfill_one(
    conn: sqlite3.Connection,
    episode: dict,
    client: LLMClient,
    buffer_n: int = DEFAULT_BUFFER_N,
    write_model: str = DEFAULT_BACKFILL_MODEL,
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


def count_pending_episodes(
    db_path: Path,
    since_episode_id: int | None = None,
    *, apply_short_skip: bool = True,
) -> int:
    """backfill 対象 (discussion_nodes 未生成の episode) の件数を返す.

    実行前に推定時間を見積もるための軽量 query (LLM 呼ばない).
    apply_short_skip=True で短文 user skip 後の実処理件数を返す.
    """
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
) -> dict[str, int]:
    """戻り値: { episodes_seen, episodes_with_nodes, nodes_added, interrupted }.

    progress=True で各 episode の N/M 進捗を stderr に表示.
    limit 指定で最大 N 件で打ち切り (= 部分的な再開実行).
    model: write LLM model 名 (default = light = gemma3:4b 相当).
    apply_short_skip: True で短文 user 発話を LLM 呼ばずに飛ばす.
    KeyboardInterrupt で抜けた場合 interrupted=1.
    """
    conn = connect(db_path)
    cli = client or OllamaClient()
    seen = 0
    with_nodes = 0
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
                    f"[backfill] {i}/{total} episode_id={ep['id']} role={ep['role']} ...\n"
                )
                sys.stderr.flush()
            try:
                n = backfill_one(
                    conn, ep, cli, buffer_n=buffer_n,
                    write_model=model, dry_run=dry_run,
                )
            except KeyboardInterrupt:
                interrupted = 1
                sys.stderr.write(
                    f"[backfill] interrupted at {i}/{total} "
                    f"(progress is saved; re-run to continue)\n"
                )
                break
            seen += 1
            if n > 0:
                with_nodes += 1
                added_total += n
                if progress:
                    sys.stderr.write(
                        f"[backfill]   → +{n} node(s) "
                        f"(cumulative {added_total})\n"
                    )
                    sys.stderr.flush()
    finally:
        conn.close()
    return {
        "episodes_seen": seen,
        "episodes_with_nodes": with_nodes,
        "nodes_added": added_total,
        "interrupted": interrupted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill discussion_nodes for past episodes (0.6.20)",
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
        "--limit", type=int, default=None,
        help="Stop after processing this many episodes (resumable). Default: all.",
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_BACKFILL_MODEL,
        help=(
            "Write LLM model for node extraction (default: "
            f"{DEFAULT_BACKFILL_MODEL} = light). "
            "Use a heavy model for higher fidelity at much slower speed."
        ),
    )
    parser.add_argument(
        "--no-short-skip", action="store_true",
        help=(
            f"Disable skipping short user messages (default skip: user content "
            f"< {SHORT_USER_SKIP_CHARS} chars). Use if short ack-like utterances "
            "are still worth scanning in your project."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be extracted without inserting nodes",
    )
    parser.add_argument(
        "--count-only", action="store_true",
        help="Print only the number of pending episodes and exit",
    )
    parser.add_argument(
        "--no-progress", action="store_true",
        help="Suppress per-episode progress (default: progress on stderr)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    apply_short_skip = not args.no_short_skip

    if args.count_only:
        n = count_pending_episodes(
            args.db, since_episode_id=args.since_episode_id,
            apply_short_skip=apply_short_skip,
        )
        print(n)
        return 0

    pending = count_pending_episodes(
        args.db, since_episode_id=args.since_episode_id,
        apply_short_skip=apply_short_skip,
    )
    targeted = pending if args.limit is None else min(pending, args.limit)
    # 経験値:
    #   - light (gemma3:4b)  : ~3-6s / episode
    #   - heavy (gemma3:12b) : ~15-30s / episode
    is_light = "4b" in args.model.lower() or "small" in args.model.lower()
    per_low, per_high = (3, 6) if is_light else (15, 30)
    est_min_low = max(1, int(targeted * per_low / 60))
    est_min_high = max(1, int(targeted * per_high / 60))
    print(f"=== discussion_nodes backfill: {args.db} ===")
    print(f"model:            {args.model}")
    print(
        f"pending episodes: {pending}"
        + (f" (limit: {args.limit})" if args.limit else "")
        + (f" (short user skip: <{SHORT_USER_SKIP_CHARS} chars)"
           if apply_short_skip else "")
    )
    print(
        f"estimated time:   {est_min_low}–{est_min_high} min "
        f"(LLM ~{per_low}–{per_high}s/episode)"
    )
    if args.dry_run:
        print("(dry-run: no DB writes)")
    if targeted == 0:
        print("Nothing to do.")
        return 0

    counts = backfill(
        args.db, since_episode_id=args.since_episode_id,
        buffer_n=args.buffer_n, dry_run=args.dry_run, limit=args.limit,
        progress=not args.no_progress,
        model=args.model,
        apply_short_skip=apply_short_skip,
    )
    print()
    print(
        f"summary: episodes_seen={counts['episodes_seen']}, "
        f"episodes_with_nodes={counts['episodes_with_nodes']}, "
        f"nodes_added={counts['nodes_added']}"
        + (" (interrupted)" if counts.get("interrupted") else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
