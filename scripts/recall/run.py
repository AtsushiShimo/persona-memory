"""recall 主エントリ (UserPromptSubmit hook の同期処理から呼ばれる).

入力: 現発話 + DB
出力: additionalContext 文字列 (空文字 = 注入なし)

仕様書 §5 に従う:
- 5.1 LLM が発話を解析 (キーワード + 履歴参照意図)
- 5.5 検索結果を LLM が関連性 curate + 自然文要約してから main に渡す
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
from scripts.recall.search import (
    bump_access_counts,
    search,
    search_episodes_by_embeddings,
)
from scripts.recall.summarize import summarize_recall
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

    # 1. LLM が発話を解析 (相槌スキップ判定 + 履歴参照意図)
    #    keywords が空 = 「OK / ありがとう」 等の相槌なので recall skip。
    #    keywords 自体は episodes 検索ヒント等のために残すが、embed 対象ではない。
    analysis = analyze_query(content, buffer, client, model=recall_model)
    if debug_enabled():
        log_keywords(content, buffer, analysis.keywords)
    if not analysis.keywords:
        if debug_enabled():
            log_final_prompt("")
        return ""

    # 2. 発話全文を 1 回 embed する (短い個別 keyword を embed すると
    #    nomic-embed-text の OOV collapse で「MVP」「Phase 1」「猫」 等が
    #    全て同じ default embedding に化け、無関係な fact と cosine 距離 0
    #    で偽 hit する。発話全文なら長く・diverse でこの問題が起きない)。
    try:
        query_emb = client.embed(embed_model, content)
    except Exception:
        query_emb = []
    if not query_emb:
        if debug_enabled():
            log_final_prompt("")
        return ""
    embeddings: list[list[float]] = [query_emb]

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
    #    facts と同じく vec0 ベクトル検索で意味的に近い episode を引く
    episodes_hits = []
    if analysis.search_history:
        episodes_hits = search_episodes_by_embeddings(conn, embeddings)

    if not hits and not episodes_hits:
        if debug_enabled():
            log_final_prompt("")
        return ""

    if hits:
        bump_access_counts(conn, [h.fact_id for h in hits])

    # 5. LLM で関連性 curate + 自然文要約 (仕様書 §5.5)
    summary = summarize_recall(content, hits, episodes_hits, client, model=recall_model)
    if not summary:
        # LLM が「関係ある記憶なし」 と判断 → 何も注入しない
        if debug_enabled():
            log_final_prompt("")
        return ""

    additional_context = f"## 思い出した記憶\n{summary}"
    if debug_enabled():
        log_final_prompt(additional_context)
    return additional_context
