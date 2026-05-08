"""fact 候補を DB に永続化 (補強 / 変更 / 新規挿入)."""
from __future__ import annotations

import sqlite3

from scripts.boot.defaults import PROTECTED_KEYS
from scripts.boot.inject import BOOT_CATEGORIES, mark_dirty
from scripts.shared.embedding import pack
from scripts.write.extract import FactCandidate
from scripts.write.similarity import Match, is_reinforcement


def _now_jst() -> str:
    """SQLite の datetime('now', '+9 hours') と同等の文字列を返す。"""
    # SQLite default で生成されるので、明示生成不要 — ここでは UPDATE 用途。
    return ""  # caller は datetime('now', '+9 hours') を SQL で使う


def insert_new(
    conn: sqlite3.Connection,
    candidate: FactCandidate,
    embedding: list[float] | None,
    source: str | None = None,
    supersedes: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO facts(category, key, value, importance, source, supersedes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (candidate.category, candidate.key, candidate.value, candidate.importance, source, supersedes),
    )
    fact_id = cur.lastrowid
    if embedding:
        conn.execute(
            "INSERT INTO fact_embeddings(fact_id, embedding) VALUES (?, ?)",
            (fact_id, pack(embedding)),
        )
    return fact_id


def reinforce(conn: sqlite3.Connection, match: Match, candidate: FactCandidate) -> None:
    """既存 fact を補強: value 更新、importance / access_count 加算、updated_at 更新。"""
    new_importance = min(9, max(match.importance, candidate.importance) + 1)
    conn.execute(
        "UPDATE facts SET "
        "  value = ?, "
        "  importance = ?, "
        "  access_count = access_count + 1, "
        "  updated_at = datetime('now', '+9 hours') "
        "WHERE id = ?",
        (candidate.value, new_importance, match.fact_id),
    )


def supersede(
    conn: sqlite3.Connection,
    match: Match,
    candidate: FactCandidate,
    embedding: list[float] | None,
    source: str | None = None,
) -> int:
    """旧 fact を superseded に降格 → 新 fact を挿入 (supersedes リンク張る)。

    UNIQUE(category, key) WHERE status='active' を守るため、
    旧の status を先に変える。

    旧 fact の embedding は fact_embeddings から削除する。残しておくと
    recall の vec0 knn 検索で古い (active 外) embedding が枠を喰い、
    active fact が漏れる現象が起きる (status は SQL 側で filter する
    が、knn の k は filter 前に決まるため)。
    """
    conn.execute("UPDATE facts SET status = 'superseded' WHERE id = ?", (match.fact_id,))
    conn.execute("DELETE FROM fact_embeddings WHERE fact_id = ?", (match.fact_id,))
    new_id = insert_new(conn, candidate, embedding, source, supersedes=match.fact_id)
    conn.execute("UPDATE facts SET superseded_by = ? WHERE id = ?", (new_id, match.fact_id))
    return new_id


def apply_candidate(
    conn: sqlite3.Connection,
    candidate: FactCandidate,
    match: Match | None,
    embedding: list[float] | None,
    source: str | None = None,
) -> str:
    """1 候補に対して 補強 / 変更 / 新規挿入 のいずれかを適用。

    boot 層 (persona / rule) を触った場合は dirty フラグを立て、次の
    UserPromptSubmit hook で即時再注入される (§8.1).

    プラグイン default として shipping されている (category, key) ペアは
    write LLM からの上書きを物理的に拒否する (PROTECTED_KEYS)。
    これにより無関係な内容で default 行動指針が破壊される事故を防ぐ。

    Returns: "reinforce" / "supersede" / "insert" / "protected"
    """
    if (candidate.category, candidate.key) in PROTECTED_KEYS:
        # default の boot 層 key は write LLM から保護
        # (例: persona/natural_voice が renju 情報で上書きされる事故を防ぐ)
        return "protected"

    if match is None:
        insert_new(conn, candidate, embedding, source)
        if candidate.category in BOOT_CATEGORIES:
            mark_dirty(conn)
        conn.commit()
        return "insert"

    if is_reinforcement(match.value, candidate.value):
        reinforce(conn, match, candidate)
        if candidate.category in BOOT_CATEGORIES:
            mark_dirty(conn)
        conn.commit()
        return "reinforce"

    if match.method == "embedding" and match.key != candidate.key:
        candidate.key = match.key
    supersede(conn, match, candidate, embedding, source)
    if candidate.category in BOOT_CATEGORIES:
        mark_dirty(conn)
    conn.commit()
    return "supersede"
