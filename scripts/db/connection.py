"""SQLite connection helper with sqlite-vec extension loaded."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

EMBEDDING_DIM = 768  # nomic-embed-text default
SCHEMA_VERSION = "3"  # cosine distance era


def connect(db_path: Path, timeout: float = 30.0) -> sqlite3.Connection:
    # write の detached child が LLM 呼び出し (10-30s) 中に conn を保持し、
    # その間に Stop hook の save_episode が `database is locked` で落ちる事象が
    # あったため、busy_timeout を 30s に拡張。WAL モード下では reader は
    # ブロックされないが writer 同士は排他なのでこの待機が必要。
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn
