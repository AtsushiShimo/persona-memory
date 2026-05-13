"""Cozo 版 recall: vec hit → topic 確定 → グラフ traverse → 流れ再構築.

設計の要点:
- topic_tag 近傍検索で関連 topic を絞る
- discussion_node 近傍検索で「流れの起点」 を見つける
- 起点から chain_from で末端まで辿り、 「○○ → △△ → □□ → 末端」 と整形
- 最後の発話 (= 末端 + 残り episodes) も併せて提示し、 main agent が
  「あの時 X と言って Y と返って...」 と自然に続けられる文脈を作る
- 既存 fact / episode vec 検索路は保持 (casual recall が壊れない)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from pycozo.client import Client

from scripts.db_cozo.discussion import (
    chain_from, find_terminal_nodes, nearest_nodes,
)
from scripts.db_cozo.repo import (
    fetch_recent_episodes_for_topic, fetch_topics,
    search_episodes_vec, search_facts_vec, search_topic_tags_vec,
)


@dataclass
class TopicCandidate:
    topic_id: str
    title: str | None
    summary: str | None
    matched_tags: list[str] = field(default_factory=list)
    hit_count: int = 0
    best_distance: float = 9.0
    last_active_at: str | None = None
    score: float = 0.0  # 複合スコア (高いほど本命)


def _compute_score(
    hit_count: int, best_distance: float,
    last_active_at: str | None, now_ts: str | None,
    unique_tag_bonus: float = 0.0,
) -> float:
    """複合スコア: hit が多く / 距離が近く / 直近に活発なほど高い.

    - hit_count: そのまま (主要 signal)
    - distance penalty: (1 - best_distance) × 3 を加算 (差を強調)
    - recency bonus: last_active_at が直近 (= 過去 7 日以内) なら +0.5
                    過去 30 日以内なら +0.2. それ以上は 0.
    - unique_tag_bonus: そのトピックでしか使われていないタグの数 × 0.5
      (= 同じタグが多 topic に付いている場合は noise, 専有タグは strong)
    """
    score = float(hit_count)
    score += max(0.0, 1.0 - best_distance) * 3.0
    score += unique_tag_bonus
    if last_active_at and now_ts:
        try:
            from datetime import datetime
            la = datetime.strptime(last_active_at, "%Y-%m-%d %H:%M:%S")
            now = datetime.strptime(now_ts, "%Y-%m-%d %H:%M:%S")
            days = (now - la).days
            if days <= 7:
                score += 0.5
            elif days <= 30:
                score += 0.2
        except Exception:
            pass
    return score


def aggregate_topic_candidates(
    tag_hits: list[dict], topics_info: dict[str, dict],
    max_topics: int = 3, now_ts: str | None = None,
) -> list[TopicCandidate]:
    """tag hit を topic_id で集約し、 複合スコア (hit + distance + recency + tag uniqueness) で並べる."""
    by_topic: dict[str, dict] = {}
    # tag → どの topic に出現したかを集計 (uniqueness 計算用)
    tag_to_topics: dict[str, set[str]] = {}
    for h in tag_hits:
        tid = h["topic_id"]
        tag = h["tag"]
        rec = by_topic.setdefault(tid, {"hits": 0, "best": 9.0, "tags": []})
        rec["hits"] += 1
        if h["distance"] < rec["best"]:
            rec["best"] = h["distance"]
        if tag not in rec["tags"]:
            rec["tags"].append(tag)
        tag_to_topics.setdefault(tag, set()).add(tid)
    if now_ts is None:
        from datetime import datetime, timedelta, timezone
        now_ts = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for tid, rec in by_topic.items():
        # この topic でしか出現していない tag の数を数える
        unique_tags = sum(
            1 for t in rec["tags"] if len(tag_to_topics.get(t, set())) == 1
        )
        info = topics_info.get(tid, {"title": None, "summary": None,
                                     "last_active_at": None})
        out.append(TopicCandidate(
            topic_id=tid,
            title=info.get("title"),
            summary=info.get("summary"),
            matched_tags=rec["tags"],
            hit_count=rec["hits"],
            best_distance=rec["best"],
            last_active_at=info.get("last_active_at"),
            score=_compute_score(
                rec["hits"], rec["best"],
                info.get("last_active_at"), now_ts,
                unique_tag_bonus=unique_tags * 0.5,
            ),
        ))
    out.sort(key=lambda c: -c.score)
    return out[:max_topics]


def has_clear_winner(
    candidates: list[TopicCandidate], margin: float = 0.20,
) -> bool:
    """単独本命と判定するか. top と 2nd の score 差が margin 以上なら True.

    margin はトップスコアに対する相対比率 (default 20%).
    """
    if not candidates:
        return False
    if len(candidates) == 1:
        return True
    top = candidates[0].score
    second = candidates[1].score
    if top <= 0:
        return False
    return (top - second) / top >= margin


def trace_flow_from(
    client: Client, start_node_ids: list[int], max_depth: int = 20,
) -> list[list[dict]]:
    """各 start_node_id から chain_from を走らせて流れの列を返す.

    戻り値: list[chain]. chain = [{depth, id, kind, title, state}, ...].
    """
    chains: list[list[dict]] = []
    for sid in start_node_ids:
        chain = chain_from(client, sid, max_depth=max_depth)
        if chain:
            chains.append(chain)
    return chains


def format_flow_block(
    candidate: TopicCandidate,
    chain: list[dict],
    last_episodes: list[dict],
) -> str:
    """1 topic に対する「流れ」 ブロックを Markdown 化.

    chain 末尾 = 議論の最後. last_episodes は新→古順で受け取り、 末端の
    生発話を 1-2 件添えて main agent が文脈を直接読めるようにする.
    """
    lines = ["## 関連する議論 — " + (candidate.title or candidate.topic_id)]
    if candidate.summary:
        lines.append(f"_{candidate.summary}_")
    if candidate.matched_tags:
        lines.append(f"一致タグ: {', '.join(candidate.matched_tags[:5])}")

    if chain:
        lines.append("")
        lines.append("**議論の流れ:**")
        for i, n in enumerate(chain):
            arrow = "  └ " if i > 0 else "- "
            head = f"#{n['id']} [{n['kind']}/{n['state']}] {n['title']}"
            lines.append(arrow + head)

    if last_episodes:
        lines.append("")
        lines.append("**最後のやり取り (新→古):**")
        for ep in last_episodes[:3]:
            snip = (ep["content"] or "").strip().replace("\n", " ")
            if len(snip) > 200:
                snip = snip[:200] + "…"
            lines.append(f"- [{ep['role']}] {snip}")

    return "\n".join(lines)


def format_multi_candidates_block(candidates: list[TopicCandidate]) -> str:
    """topic 候補が複数で確信が薄い時、 main agent に聞き返しを促すブロック."""
    lines = ["## 関連する議論 (候補複数)"]
    for c in candidates:
        head = c.title or c.topic_id
        lines.append(f"- **{head}** (topic_id={c.topic_id}, hit={c.hit_count})")
        if c.summary:
            sn = c.summary.replace("\n", " ")
            if len(sn) > 80:
                sn = sn[:80] + "…"
            lines.append(f"  └ {sn}")
        if c.matched_tags:
            lines.append(f"  └ タグ: {', '.join(c.matched_tags[:5])}")
    lines.append("")
    lines.append("## 候補確認")
    lines.append(
        "上記候補から絞り込めない場合、 ユーザーに『どの話題でしょうか?』 と"
        "聞き返してください. 確定したら "
        "`mcp__persona-memory__continue_topic(topic_id=...)` を呼んで以後の"
        "発話を当該 topic に紐付けてください."
    )
    return "\n".join(lines)


def recall_topic_flow(
    client: Client,
    query_embedding: list[float],
    *,
    tag_top_k: int = 12,
    tag_distance_max: float = 0.6,
    node_top_k: int = 5,
    node_distance_max: float = 0.6,
    max_topics: int = 3,
    last_n_episodes: int = 3,
) -> str:
    """発話 embedding を起点に topic + 流れ + 最後の発話を組み立てる.

    戻り値: additionalContext に注入する Markdown 文字列. 候補無しなら "".
    """
    if not query_embedding:
        return ""
    tag_hits = search_topic_tags_vec(
        client, query_embedding, top_k=tag_top_k, distance_max=tag_distance_max,
    )
    if not tag_hits:
        return ""
    topic_ids = list({h["topic_id"] for h in tag_hits})
    topics_info = fetch_topics(client, topic_ids)
    candidates = aggregate_topic_candidates(tag_hits, topics_info, max_topics=max_topics)
    if not candidates:
        return ""

    if not has_clear_winner(candidates, margin=0.20):
        # スコア接戦 → 候補確認に倒す
        return format_multi_candidates_block(candidates)

    # 単独本命候補: 流れ再構築
    top = candidates[0]
    node_hits = nearest_nodes(
        client, query_embedding, top_k=node_top_k, distance_max=node_distance_max,
    )
    chain: list[dict] = []
    if node_hits:
        # トップ node から chain_from
        chain = chain_from(client, node_hits[0]["id"])
    last_eps = fetch_recent_episodes_for_topic(client, top.topic_id, last_n=last_n_episodes)
    return format_flow_block(top, chain, last_eps)
