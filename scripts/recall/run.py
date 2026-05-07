"""recall 主エントリ (UserPromptSubmit hook の同期処理から呼ばれる).

入力: 現発話 + DB
出力: additionalContext 文字列 (空文字 = 注入なし)

仕様書 §5.1 に対応: LLM が発話を解析 (キーワード + 履歴参照意図).
仕様書 §5.5 (要約) は **次の段階で実装予定**、現状は素のリストを返す。
"""
from __future__ import annotations

import os
import sqlite3

from scripts.debug.recall_log import (
    is_enabled as debug_enabled,
    log_final_prompt,
    log_hits,
    log_keywords,
)
from scripts.recall.extract import RECALL_MODEL, analyze_query
from scripts.recall.format import to_additional_context
from scripts.recall.search import (
    bump_access_counts,
    search,
    search_episodes_by_keywords,
)
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
    """ユーザー発話から関連記憶を引き、要約 additionalContext を返す。"""
    if not content.strip():
        return ""
    buffer = fetch_buffer(conn, buffer_n)

    # 1. LLM が発話を解析 (キーワード + 履歴参照意図)
    analysis = analyze_query(content, buffer, client, model=recall_model)
    if debug_enabled():
        log_keywords(content, buffer, analysis.keywords)
    if not analysis.keywords:
        if debug_enabled():
            log_final_prompt("")
        return ""

    # 2. 各キーワードを embed
    embeddings: list[list[float]] = []
    for kw in analysis.keywords:
        try:
            emb = client.embed(embed_model, kw)
            if emb:
                embeddings.append(emb)
        except Exception:
            continue
    if not embeddings:
        if debug_enabled():
            log_final_prompt("")
        return ""

    # 3. facts 検索
    hits = search(conn, embeddings)
    if debug_enabled():
        log_hits(len(analysis.keywords), [
            {
                "fact_id": h.fact_id, "category": h.category, "key": h.key,
                "distance": round(h.distance, 4), "importance": h.importance,
                "score": round(h.score, 4),
            }
            for h in hits
        ])

    # 4. episodes 検索 (LLM が「履歴参照」 と判断したとき)
    episodes_hits = []
    if analysis.search_history:
        episodes_hits = search_episodes_by_keywords(conn, analysis.keywords)

    if not hits and not episodes_hits:
        if debug_enabled():
            log_final_prompt("")
        return ""

    if hits:
        bump_access_counts(conn, [h.fact_id for h in hits])
    additional_context = to_additional_context(hits, episodes_hits)
    if debug_enabled():
        log_final_prompt(additional_context)
    return additional_context
