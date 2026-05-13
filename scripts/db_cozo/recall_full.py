"""Cozo メインのフル recall パイプライン.

旧 SQLite scripts.recall.run.recall を置き換え:
- analyze_query (LLM): キーワード抽出 + search_history 判定
- search_facts (Cozo HNSW): 発話 embedding に近い active fact
- search_episodes (Cozo HNSW): 過去参照系 query なら episodes も
- summarize_recall (LLM): facts + episodes を curate して自然文へ
- topic flow block (Cozo グラフ): 「## 関連する議論」 + 流れ + 最後のやり取り

加えて boot 層 dirty 再注入は SQLite recall 同様に行う (boot 層は別マスター).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from pycozo.client import Client

from scripts.db_cozo.recall import (
    aggregate_topic_candidates, format_flow_block,
    format_multi_candidates_block, has_clear_winner,
)
from scripts.db_cozo.repo import (
    fetch_recent_episodes_for_topic, fetch_topics,
    search_episodes_vec, search_facts_vec, search_topic_tags_vec,
)
from scripts.db_cozo.discussion import chain_from, nearest_nodes
from scripts.recall.extract import RECALL_NUM_CTX, analyze_query
from scripts.recall.search import RecalledEpisode, RecalledFact
from scripts.recall.summarize import summarize_recall
from scripts.shared.ollama import LLMClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")


def _to_recalled_facts(rows: list[dict]) -> list[RecalledFact]:
    """db_cozo.repo.search_facts_vec 結果を summarize_recall 用 dataclass へ."""
    out = []
    for r in rows:
        out.append(RecalledFact(
            fact_id=r["id"], category=r["category"], key=r["key"],
            value=r["value"], importance=r["importance"],
            access_count=0, distance=r["distance"],
            score=max(0.0, 1.0 - r["distance"]),
        ))
    return out


def _to_recalled_episodes(rows: list[dict]) -> list[RecalledEpisode]:
    out = []
    for r in rows:
        out.append(RecalledEpisode(
            episode_id=r["id"], role=r["role"],
            content=r["content"], timestamp=r.get("timestamp", ""),
        ))
    return out


def recall_full(
    client: Client, content: str, llm: LLMClient,
    *, buffer: list[dict] | None = None,
) -> str:
    """Cozo メインのフル recall.  additionalContext (Markdown) を返す.

    buffer は analyze_query に渡す直近会話 (= context-aware keyword extraction).
    """
    buffer = buffer or []
    # 1. analyze_query
    try:
        analysis = analyze_query(content, buffer, llm)
    except Exception as e:
        sys.stderr.write(f"[persona-memory] cozo analyze_query failed: {e}\n")
        return ""

    # 2. embed query
    try:
        q_emb = llm.embed(EMBED_MODEL, content) or []
    except Exception:
        q_emb = []
    if not q_emb:
        return ""

    # 3. search facts (vec)
    fact_rows = []
    try:
        fact_rows = search_facts_vec(client, q_emb, top_k=10, distance_max=0.6)
    except Exception as e:
        sys.stderr.write(f"[persona-memory] cozo search_facts failed: {e}\n")

    # 4. search episodes (only when LLM judges past-reference query)
    episode_rows = []
    if analysis.search_history:
        try:
            episode_rows = search_episodes_vec(
                client, q_emb, top_k=5, distance_max=0.6,
            )
        except Exception as e:
            sys.stderr.write(f"[persona-memory] cozo search_episodes failed: {e}\n")

    # 5. topic flow block
    topic_block = ""
    try:
        tag_hits = search_topic_tags_vec(client, q_emb, top_k=12, distance_max=0.6)
        if tag_hits:
            ids = list({h["topic_id"] for h in tag_hits})
            info = fetch_topics(client, ids)
            cands = aggregate_topic_candidates(tag_hits, info)
            if cands:
                if not has_clear_winner(cands, margin=0.20):
                    topic_block = format_multi_candidates_block(cands)
                else:
                    top = cands[0]
                    node_hits = nearest_nodes(client, q_emb, top_k=5, distance_max=0.6)
                    chain = chain_from(client, node_hits[0]["id"]) if node_hits else []
                    last_eps = fetch_recent_episodes_for_topic(
                        client, top.topic_id, last_n=3,
                    )
                    topic_block = format_flow_block(top, chain, last_eps)
    except Exception as e:
        sys.stderr.write(f"[persona-memory] cozo topic flow failed: {e}\n")

    # 6. summarize facts + episodes (旧 recall と同じ summarize_recall を使う)
    summary_block = ""
    if fact_rows or episode_rows:
        try:
            summary = summarize_recall(
                content,
                _to_recalled_facts(fact_rows),
                _to_recalled_episodes(episode_rows),
                llm,
            )
            if summary and summary != "(該当なし)":
                summary_block = f"## 思い出した記憶\n{summary}"
        except Exception as e:
            sys.stderr.write(f"[persona-memory] cozo summarize failed: {e}\n")

    # 7. compose
    sections = [s for s in (topic_block, summary_block) if s]
    return "\n\n".join(sections)
