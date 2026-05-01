"""SQLite + sqlite-vec helpers for persona memory."""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

DB_PATH_ENV = "PERSONA_MEMORY_DB"

_PERSONA_NAME_BRACKET_RE = re.compile(r"[『「](.+?)[』」]")


def get_persona_name(default: str = "AI アシスタント") -> str:
    """Return the persona's display name from `persona/identity` fact.

    The fact's free-text value typically contains the name in 『』 or 「」
    brackets (e.g. "このペルソナの名前は『〈name〉』"). Falls back to
    `default` if the fact is absent, the value lacks bracketed content, or
    any DB error occurs. This lets distributable scripts reference the
    assistant by name without hard-coding it in source.
    """
    try:
        with connect() as c:
            row = c.execute(
                "SELECT value FROM facts WHERE category = 'persona' "
                "AND key = 'identity' AND status = 'active'"
            ).fetchone()
        if not row:
            return default
        m = _PERSONA_NAME_BRACKET_RE.search(row["value"] or "")
        if m:
            return m.group(1).strip() or default
        return default
    except Exception:
        return default


def db_path() -> Path:
    raw = os.environ.get(DB_PATH_ENV)
    if not raw:
        raise RuntimeError(
            f"Set {DB_PATH_ENV} to the persona memory DB path "
            f"(e.g. /path/to/persona.db)."
        )
    p = Path(raw).expanduser()
    if not p.exists():
        raise RuntimeError(
            f"DB not found: {p}. Run scripts/init-memory.py {p} first."
        )
    return p


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_meta(key: str) -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def upsert_fact(
    *,
    category: str,
    key: str,
    value: str,
    importance: int,
    source: str | None,
) -> int:
    # 既存 DB の DEFAULT は UTC のままなので、INSERT 側でも明示的に JST を渡す。
    # 新規 DB は schema.sql の DEFAULT (`datetime('now', '+9 hours')`) でも JST。
    with connect() as c:
        cur = c.execute(
            """
            INSERT INTO facts(category, key, value, importance, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', '+9 hours'), datetime('now', '+9 hours'))
            ON CONFLICT(category, key) DO UPDATE SET
                value = excluded.value,
                importance = excluded.importance,
                source = excluded.source,
                status = 'active',
                updated_at = datetime('now', '+9 hours')
            RETURNING id
            """,
            (category, key, value, importance, source),
        )
        return int(cur.fetchone()["id"])


def write_fact_embedding(fact_id: int, embedding_blob: bytes) -> None:
    with connect() as c:
        c.execute("DELETE FROM facts_vec WHERE fact_id = ?", (fact_id,))
        c.execute(
            "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
            (fact_id, embedding_blob),
        )


def search_facts(
    *,
    embedding_blob: bytes,
    top_k: int,
    category: str | None,
) -> list[dict]:
    sql = """
        SELECT
            f.id, f.category, f.key, f.value, f.importance, f.status,
            v.distance
        FROM facts_vec v
        JOIN facts f ON f.id = v.fact_id
        WHERE v.embedding MATCH ?
          AND k = ?
          AND f.status != 'superseded'
    """
    params: list = [embedding_blob, top_k]
    if category:
        sql += " AND f.category = ?"
        params.append(category)
    sql += " ORDER BY v.distance"

    with connect() as c:
        rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def list_active_facts(limit: int = 1000) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT id, category, key, value FROM facts "
            "WHERE status = 'active' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_fact(fact_id: int) -> dict | None:
    with connect() as c:
        row = c.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return dict(row) if row else None


def find_neighbors(fact_id: int, embedding_blob: bytes, top_k: int) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """
            SELECT v.fact_id, v.distance, f.category, f.key, f.value
            FROM facts_vec v
            JOIN facts f ON f.id = v.fact_id
            WHERE v.embedding MATCH ?
              AND k = ?
              AND f.status = 'active'
              AND v.fact_id != ?
            ORDER BY v.distance
            """,
            (embedding_blob, top_k, fact_id),
        ).fetchall()
        return [dict(r) for r in rows]


def flag_conflict(
    *,
    fact_a_id: int,
    fact_b_id: int,
    confidence: int,
    auto_resolved: bool,
) -> None:
    with connect() as c:
        c.execute(
            """
            INSERT INTO conflicts(fact_a_id, fact_b_id, confidence, resolution, detected_at)
            VALUES (?, ?, ?, ?, datetime('now', '+9 hours'))
            """,
            (
                fact_a_id,
                fact_b_id,
                confidence,
                "auto_superseded" if auto_resolved else "pending",
            ),
        )
        if auto_resolved:
            c.execute(
                "UPDATE facts SET status = 'superseded', updated_at = datetime('now', '+9 hours') "
                "WHERE id = ?",
                (fact_b_id,),
            )
        else:
            c.execute(
                "UPDATE facts SET status = 'conflict', updated_at = datetime('now', '+9 hours') "
                "WHERE id IN (?, ?)",
                (fact_a_id, fact_b_id),
            )


def record_lint_run(
    *, pairs_examined: int, conflicts_flagged: int, conflicts_auto_resolved: int
) -> None:
    with connect() as c:
        c.execute(
            """
            INSERT INTO lint_log(pairs_examined, conflicts_flagged, conflicts_auto_resolved, run_at)
            VALUES (?, ?, ?, datetime('now', '+9 hours'))
            """,
            (pairs_examined, conflicts_flagged, conflicts_auto_resolved),
        )


def forget_fact(*, category: str, key: str) -> dict | None:
    """Soft-delete: mark fact as 'superseded'. Searchable history preserved."""
    with connect() as c:
        row = c.execute(
            "SELECT id, value FROM facts WHERE category = ? AND key = ? AND status = 'active'",
            (category, key),
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE facts SET status = 'superseded', updated_at = datetime('now', '+9 hours') WHERE id = ?",
            (row["id"],),
        )
        return {"fact_id": row["id"], "previous_value": row["value"]}


def delete_fact(*, fact_id: int) -> dict | None:
    """Hard-delete: remove fact + embedding row entirely. Irreversible."""
    with connect() as c:
        row = c.execute(
            "SELECT category, key, value FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
        if not row:
            return None
        c.execute("DELETE FROM facts_vec WHERE fact_id = ?", (fact_id,))
        c.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        return {
            "fact_id": fact_id,
            "category": row["category"],
            "key": row["key"],
            "previous_value": row["value"],
        }


def append_episode(
    *,
    session_id: str | None,
    role: str,
    content: str,
    summary: str | None,
) -> int:
    # JST を明示 (legacy DB は DEFAULT が UTC のままなので)
    with connect() as c:
        cur = c.execute(
            "INSERT INTO episodes(session_id, role, content, summary, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now', '+9 hours')) RETURNING id",
            (session_id, role, content, summary),
        )
        return int(cur.fetchone()["id"])


def write_episode_embedding(episode_id: int, embedding_blob: bytes) -> None:
    with connect() as c:
        c.execute("DELETE FROM episodes_vec WHERE episode_id = ?", (episode_id,))
        c.execute(
            "INSERT INTO episodes_vec(episode_id, embedding) VALUES (?, ?)",
            (episode_id, embedding_blob),
        )


def search_episodes(*, embedding_blob: bytes, top_k: int) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            """
            SELECT e.id, e.session_id, e.role, e.summary, e.created_at, v.distance
            FROM episodes_vec v
            JOIN episodes e ON e.id = v.episode_id
            WHERE v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
            """,
            (embedding_blob, top_k),
        ).fetchall()
        return [dict(r) for r in rows]
