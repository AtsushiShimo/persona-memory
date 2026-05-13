"""topic_tag 近傍検索 + topic_id 集計.

recall パイプラインから呼ばれる. 発話 embedding に近い topic_tag を引いて、
それらが指す topic_id を集計する. 同 topic に複数 tag が hit すれば signal が
強い (= 本命候補).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from scripts.shared.embedding import pack


@dataclass
class TopicCandidate:
    topic_id: str
    title: str | None
    summary: str | None
    hit_count: int
    best_distance: float
    matched_tags: list[str]


def search_topic_candidates(
    conn: sqlite3.Connection,
    query_embedding: list[float],
    top_k_tags: int = 12,
    distance_max: float = 0.6,
    max_topics: int = 3,
) -> list[TopicCandidate]:
    """発話 embedding から関連 topic 候補を返す (新しい順 + signal 強い順).

    手順:
      1. topic_tag_embeddings を vec0 cosine で top_k_tags 件検索
      2. distance_max でフィルタ
      3. topic_id 単位で集約 (hit_count = 同 topic に hit した tag 数)
      4. 強い順 (hit_count DESC, best_distance ASC) で max_topics 件返す
    """
    if not query_embedding:
        return []
    blob = pack(query_embedding)
    try:
        rows = conn.execute(
            """
            SELECT t.tag, t.topic_id, v.distance
            FROM topic_tag_embeddings v
            JOIN topic_tags t ON t.id = v.topic_tag_id
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
            """,
            (blob, top_k_tags),
        ).fetchall()
    except sqlite3.OperationalError:
        # topic_tag_embeddings テーブル無い古い DB は無視
        return []

    by_topic: dict[str, dict] = {}
    for tag, topic_id, distance in rows:
        if distance > distance_max:
            continue
        rec = by_topic.setdefault(topic_id, {"hits": 0, "best": 9.0, "tags": []})
        rec["hits"] += 1
        if distance < rec["best"]:
            rec["best"] = distance
        if tag not in rec["tags"]:
            rec["tags"].append(tag)

    if not by_topic:
        return []

    # topics 本体を引いて title/summary を付与
    topic_ids = list(by_topic.keys())
    placeholders = ",".join("?" * len(topic_ids))
    info_rows = conn.execute(
        f"SELECT id, title, summary FROM topics WHERE id IN ({placeholders})",
        topic_ids,
    ).fetchall()
    info = {r[0]: (r[1], r[2]) for r in info_rows}

    candidates: list[TopicCandidate] = []
    for tid, rec in by_topic.items():
        title, summary = info.get(tid, (None, None))
        candidates.append(TopicCandidate(
            topic_id=tid, title=title, summary=summary,
            hit_count=rec["hits"], best_distance=rec["best"],
            matched_tags=list(rec["tags"]),
        ))

    candidates.sort(key=lambda c: (-c.hit_count, c.best_distance))
    return candidates[:max_topics]


def format_topic_block(candidates: list[TopicCandidate]) -> str:
    """recall additionalContext 用の「## 関連する議論」 ブロックを生成.

    候補 1 件: title + summary を出す.
    候補複数: 各候補の見出し + 「## 候補確認」 セクションで main agent に
    聞き返し指示.
    """
    if not candidates:
        return ""
    if len(candidates) == 1:
        c = candidates[0]
        lines = ["## 関連する議論"]
        head = c.title or "(無題の話題)"
        lines.append(f"- **{head}** (topic_id={c.topic_id})")
        if c.matched_tags:
            lines.append(f"  └ 一致タグ: {', '.join(c.matched_tags[:5])}")
        if c.summary:
            lines.append(f"  └ 要約: {c.summary}")
        else:
            lines.append("  └ (要約は SessionEnd 時に生成. 未生成のため tag のみ提示)")
        return "\n".join(lines)

    # 複数候補
    lines = ["## 関連する議論 (候補複数)"]
    for c in candidates:
        head = c.title or "(無題)"
        lines.append(
            f"- **{head}** (topic_id={c.topic_id}, hit={c.hit_count})"
        )
        if c.matched_tags:
            lines.append(f"  └ タグ: {', '.join(c.matched_tags[:5])}")
        if c.summary:
            sn = c.summary.replace("\n", " ")
            if len(sn) > 80:
                sn = sn[:80] + "…"
            lines.append(f"  └ {sn}")

    lines.append("")
    lines.append("## 候補確認")
    lines.append(
        "上記の候補から main agent が単独に絞り込めない場合は、"
        " ユーザーに『どの話題でしょうか?』 と聞き返してください。"
        " 確定したら mcp__persona-memory__continue_topic(topic_id=...) を呼んで"
        " 以後の発話を当該 topic に紐付けてください。"
    )
    return "\n".join(lines)
