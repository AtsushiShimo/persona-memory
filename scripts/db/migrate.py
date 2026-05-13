"""Apply the redesign schema to a fresh DB."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from scripts.db.connection import EMBEDDING_DIM, SCHEMA_VERSION, connect

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def init_db(db_path: Path, embedding_dim: int = EMBEDDING_DIM) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        with open(SCHEMA_FILE) as f:
            conn.executescript(f.read())

        # distance_metric=cosine: nomic-embed-text は非正規化 vector を返すため
        # L2 (vec0 default) では semantic similarity が判別できない。cosine 指定で
        # スケール非依存・方向のみの距離計算に切り替える (構文確認済み: sqlite-vec 0.1.9)
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS fact_embeddings USING vec0("
            f"  fact_id INTEGER PRIMARY KEY,"
            f"  embedding FLOAT[{embedding_dim}] distance_metric=cosine"
            f")"
        )
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS episode_embeddings USING vec0("
            f"  episode_id INTEGER PRIMARY KEY,"
            f"  embedding FLOAT[{embedding_dim}] distance_metric=cosine"
            f")"
        )
        # 0.6.24 トピック記憶: tag 単体を embed → topic_id 集計用.
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS topic_tag_embeddings USING vec0("
            f"  topic_tag_id INTEGER PRIMARY KEY,"
            f"  embedding FLOAT[{embedding_dim}] distance_metric=cosine"
            f")"
        )

        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("embedding_dim", str(embedding_dim)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        conn.commit()
    finally:
        conn.close()

    os.chmod(db_path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize persona-memory DB (redesign schema)")
    parser.add_argument("db_path", type=Path, help="Path to the SQLite DB")
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=EMBEDDING_DIM,
        help=f"Embedding vector dimension (default: {EMBEDDING_DIM})",
    )
    args = parser.parse_args()

    if args.db_path.exists():
        print(f"DB already exists: {args.db_path}", file=sys.stderr)
        print("Refusing to overwrite. Delete it manually for a fresh start.", file=sys.stderr)
        return 1

    init_db(args.db_path, args.embedding_dim)
    print(f"Initialized: {args.db_path}")
    print(f"  schema:        {SCHEMA_FILE}")
    print(f"  embedding_dim: {args.embedding_dim}")
    print(f"  schema_version: {SCHEMA_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
