"""recall 主エントリ (UserPromptSubmit hook の同期処理から呼ばれる).

入力: 現発話 + DB
出力: additionalContext 文字列 (空文字 = 注入なし)
"""
from __future__ import annotations

import os
import sqlite3

from scripts.recall.extract import RECALL_MODEL, extract_query_keywords
from scripts.recall.format import to_additional_context
from scripts.recall.search import bump_access_counts, search
from scripts.shared.ollama import LLMClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
BUFFER_N = int(os.environ.get("PERSONA_BUFFER_N", "3"))


def fetch_buffer(conn: sqlite3.Connection, n: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM episodes ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def recall(
    conn: sqlite3.Connection,
    content: str,
    client: LLMClient,
    buffer_n: int = BUFFER_N,
    recall_model: str = RECALL_MODEL,
    embed_model: str = EMBED_MODEL,
) -> str:
    """ユーザー発話から関連 fact を引き、additionalContext を返す。"""
    if not content.strip():
        return ""
    buffer = fetch_buffer(conn, buffer_n)

    keywords = extract_query_keywords(content, buffer, client, model=recall_model)
    if not keywords:
        return ""

    embeddings: list[list[float]] = []
    for kw in keywords:
        try:
            emb = client.embed(embed_model, kw)
            if emb:
                embeddings.append(emb)
        except Exception:
            continue
    if not embeddings:
        return ""

    hits = search(conn, embeddings)
    if not hits:
        return ""

    bump_access_counts(conn, [h.fact_id for h in hits])
    return to_additional_context(hits)
