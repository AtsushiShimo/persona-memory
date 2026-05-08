#!/usr/bin/env python3
"""recall パイプラインを既存 DB に対してオフライン実行する dev ツール.

目的: プロンプト調整ループの短縮。Claude Code の再起動 + plugin install を
挟まずに、prompt 編集 → このスクリプト実行 で recall の挙動が確認できる。

挙動:
- 指定 DB に接続 (read-only マウント)
- 指定発話 (= 現発話) と直近会話バッファ (= 既存 episodes 末尾 N 件) で:
  1. analyze_query (LLM): キーワード + search_history 抽出
  2. embed (LLM): 各キーワードを embed
  3. search facts + episodes (vec0)
  4. summarize_recall (LLM): 関連性 curate + 要約
- 各段の prompt / response / hits を全て stdout に表示
- DB は読み取りのみ (access_count 更新もしない)

Usage:
  PYTHONPATH=. .venv/bin/python scripts/tools/recall_replay.py \\
    --db ~/Desktop/claude_dev/persona-test3/.persona-memory/ソフィア.db \\
    --query 'ペットの名前覚えてる？'
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from scripts.db.connection import connect
from scripts.recall.extract import RECALL_MODEL, analyze_query
from scripts.recall.search import (
    EPISODE_TARGET_HITS,
    search,
    search_episodes_by_embeddings,
)
from scripts.recall.summarize import build_summarize_prompt, summarize_recall
from scripts.shared.ollama import OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")


def fetch_buffer(conn, n: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM episodes ORDER BY id DESC LIMIT ?", (n,),
    ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def _print_section(title: str):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def main() -> int:
    p = argparse.ArgumentParser(description="Recall pipeline を offline で再現")
    p.add_argument("--db", required=True, type=Path, help="persona DB path")
    p.add_argument("--query", required=True, help="現発話")
    p.add_argument("--buffer-n", type=int, default=3,
                   help="直近会話バッファ件数 (default: 3)")
    p.add_argument("--show-prompt", action="store_true", default=True,
                   help="各 LLM への prompt 全文を表示 (default ON)")
    p.add_argument("--no-show-prompt", action="store_false", dest="show_prompt")
    args = p.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        return 2

    conn = connect(args.db)
    client = OllamaClient()

    buffer = fetch_buffer(conn, args.buffer_n)

    _print_section(f"Stage 1: analyze_query (model={RECALL_MODEL})")
    print(f"query:    {args.query}")
    print(f"buffer ({len(buffer)} turns):")
    for b in buffer:
        c = b["content"]
        if len(c) > 80:
            c = c[:80] + "…"
        print(f"  [{b['role']}] {c}")
    analysis = analyze_query(args.query, buffer, client)
    print()
    print(f"-> keywords:       {analysis.keywords}  (相槌 skip 判定 + episode hint 用、embed には使わない)")
    print(f"-> search_history: {analysis.search_history}")
    if not analysis.keywords:
        print("\n(キーワードが空 = 相槌のため終了)")
        return 0

    _print_section("Stage 2: embed query (発話全文 1 つ)")
    try:
        v = client.embed(EMBED_MODEL, args.query)
        ok = bool(v) and len(v) == 768
        print(f"  query全文: dim={len(v) if v else 0} {'OK' if ok else 'EMPTY'}")
        embeddings: list[list[float]] = [v] if v else []
    except Exception as e:
        print(f"  query全文: ERROR {e}")
        embeddings = []

    _print_section("Stage 3a: search facts (vec0 cosine)")
    fact_hits = search(conn, embeddings)
    if not fact_hits:
        print("(no fact hits)")
    for h in fact_hits:
        v = h.value
        if len(v) > 80:
            v = v[:80] + "…"
        print(f"  d={h.distance:.4f} score={h.score:.4f} imp={h.importance} "
              f"[{h.category}/{h.key}] {v}")

    _print_section("Stage 3b: search episodes (vec0 cosine, "
                   f"target={EPISODE_TARGET_HITS})")
    if not analysis.search_history:
        print("(search_history=False のため skip)")
        episodes_hits = []
    else:
        episodes_hits = search_episodes_by_embeddings(conn, embeddings)
        if not episodes_hits:
            print("(no episode hits within hard threshold)")
        for h in episodes_hits:
            c = h.content
            if len(c) > 80:
                c = c[:80] + "…"
            print(f"  [{h.timestamp}] [{h.role}] {c}")

    if not fact_hits and not episodes_hits:
        print("\n(facts も episodes も空のため summarize 段は skip)")
        return 0

    _print_section(f"Stage 4: summarize_recall (model={RECALL_MODEL})")
    if args.show_prompt:
        prompt = build_summarize_prompt(args.query, fact_hits, episodes_hits)
        print("--- prompt ---")
        print(prompt)
        print()
    summary = summarize_recall(args.query, fact_hits, episodes_hits, client)
    print("--- response ---")
    print(summary if summary else "(empty / 該当なし)")

    _print_section("Stage 5: final additionalContext")
    if summary:
        print(f"## 思い出した記憶\n{summary}")
    else:
        print("(empty — メインに何も注入されない)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
