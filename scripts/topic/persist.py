"""topic_tags と topic_tag_embeddings への保存."""
from __future__ import annotations

import sqlite3

from scripts.shared.embedding import pack
from scripts.shared.ollama import LLMClient
from scripts.topic.state import ensure_topic

EMBED_MODEL_DEFAULT = "nomic-embed-text"


def save_tags(
    conn: sqlite3.Connection,
    topic_id: str,
    tags: list[str],
    client: LLMClient,
    embed_model: str = EMBED_MODEL_DEFAULT,
) -> int:
    """tags を topic_tags + topic_tag_embeddings へ保存. 保存件数を返す.

    fail-open: embedding 失敗 (Ollama 落ち等) 時は tag 行のみ保存して続行.
    """
    if not tags or not topic_id:
        return 0
    ensure_topic(conn, topic_id)
    saved = 0
    for tag in tags:
        cur = conn.execute(
            "INSERT INTO topic_tags(topic_id, tag) VALUES (?, ?)",
            (topic_id, tag),
        )
        tag_id = cur.lastrowid
        # embed text は tag 単体. recall 側も発話 → embed → topic_tag_embeddings に
        # 近傍検索する設計なので、 tag 単体で良い (cat/key 形式は不要).
        try:
            vec = client.embed(embed_model, tag)
        except Exception:
            vec = []
        if vec:
            try:
                conn.execute(
                    "INSERT INTO topic_tag_embeddings(topic_tag_id, embedding) VALUES (?, ?)",
                    (tag_id, pack(vec)),
                )
            except Exception:
                pass
        saved += 1
    conn.commit()
    return saved
