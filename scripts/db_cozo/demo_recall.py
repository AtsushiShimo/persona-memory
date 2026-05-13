"""Cozo 版 recall を CLI で試すデモ.

migrate_from_sqlite + backfill_graph 後の DB に対して、 任意 query で
「## 関連する議論 + 流れ」 を出力する. Sofia のフックを書き換える前に
algorithm が動くかを手元で確かめる用途.

実行例:
  python -m scripts.db_cozo.demo_recall \\
      --db ~/.persona-memory/<persona>.cozo.db \\
      --query "Renju の話を再開しよう"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.recall import recall_topic_flow
from scripts.shared.ollama import OllamaClient

EMBED_MODEL_DEFAULT = "nomic-embed-text"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--query", required=True)
    p.add_argument("--embed-model", default=EMBED_MODEL_DEFAULT)
    args = p.parse_args(argv)
    if not args.db.exists():
        sys.stderr.write(f"db not found: {args.db}\n")
        return 1
    client = init_db(args.db)
    llm = OllamaClient()
    try:
        emb = llm.embed(args.embed_model, args.query)
    except Exception as e:
        sys.stderr.write(f"embed failed: {e}\n")
        return 2
    out = recall_topic_flow(client, emb)
    if not out:
        print("(該当する関連議論が見つかりませんでした)")
        return 0
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
