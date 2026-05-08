"""SQLite connection helper with sqlite-vec extension loaded."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

EMBEDDING_DIM = 768  # nomic-embed-text default
SCHEMA_VERSION = "3"  # cosine distance era


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
