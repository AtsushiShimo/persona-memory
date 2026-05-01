#!/usr/bin/env python3
"""Initialize a persona memory DB with schema + sqlite-vec virtual tables."""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

import sqlite_vec

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "db" / "schema.sql"
EMBEDDING_DIM = 768  # nomic-embed-text default


def init_db(db_path: Path, embedding_dim: int) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    with open(SCHEMA_FILE) as f:
        conn.executescript(f.read())

    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS facts_vec USING vec0("
        f"  fact_id  INTEGER PRIMARY KEY,"
        f"  embedding FLOAT[{embedding_dim}]"
        f")"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS episodes_vec USING vec0("
        f"  episode_id INTEGER PRIMARY KEY,"
        f"  embedding  FLOAT[{embedding_dim}]"
        f")"
    )

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("embedding_dim", str(embedding_dim)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", "1"),
    )
    conn.commit()
    conn.close()

    os.chmod(db_path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize persona memory DB")
    parser.add_argument(
        "db_path",
        type=Path,
        help="Path to the SQLite DB (e.g. ./data/persona.db)",
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=EMBEDDING_DIM,
        help=f"Embedding vector dimension (default: {EMBEDDING_DIM})",
    )
    args = parser.parse_args()

    if args.db_path.exists():
        print(f"DB already exists: {args.db_path}", file=sys.stderr)
        print("Refusing to overwrite. Delete it manually if you want a fresh start.", file=sys.stderr)
        return 1

    init_db(args.db_path, args.embedding_dim)
    print(f"Initialized: {args.db_path}")
    print(f"  schema: {SCHEMA_FILE}")
    print(f"  embedding_dim: {args.embedding_dim}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
