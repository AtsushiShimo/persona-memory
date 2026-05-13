"""topic.title + topic.summary を 1 LLM コールで生成.

SessionEnd: 当該 session の topic_id の episodes を集めて 1 回 LLM に投げる.
SessionStart fallback: 過去 topic で summary が空のものを 1 件遡及生成.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3

from scripts.shared.ollama import LLMClient

TOPIC_SUMMARY_MODEL = os.environ.get(
    "PERSONA_TOPIC_SUMMARY_MODEL",
    os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b"),
)
# topic に紐付く全 episodes を渡す前提で広めの context.
TOPIC_SUMMARY_NUM_CTX = int(os.environ.get("PERSONA_TOPIC_SUMMARY_NUM_CTX", "16384"))
# 1 topic あたり要約に渡す最大 episode 数 (古い順から最新側を優先).
TOPIC_SUMMARY_MAX_EPISODES = int(os.environ.get("PERSONA_TOPIC_SUMMARY_MAX_EPISODES", "60"))


_PROMPT_TEMPLATE = """\
以下は 1 つの議題に紐付いた発話ログです. これを後で「あの話どうだったっけ」 と
思い出すために, **タイトル (15 字以内) と 要約 (3-5 行)** を生成してください.

要約には以下を含めてください:
- 主題語 (例: "Renju のサイドバー UI")
- どの論点が挙がったか
- どこまで決まったか / 何が未決か

出力は JSON のみ (説明・前置き・コードフェンス禁止):
{{"title": "...", "summary": "..."}}

発話ログ:
{episodes}
"""


def build_prompt(episode_lines: list[str]) -> str:
    body = "\n".join(episode_lines) if episode_lines else "(発話なし)"
    return _PROMPT_TEMPLATE.format(episodes=body)


def parse_summary(text: str) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None, None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    title = data.get("title")
    summary = data.get("summary")
    if isinstance(title, str):
        title = title.strip()[:60] or None
    else:
        title = None
    if isinstance(summary, str):
        summary = summary.strip() or None
    else:
        summary = None
    return title, summary


def fetch_topic_episodes(
    conn: sqlite3.Connection, topic_id: str, max_episodes: int = TOPIC_SUMMARY_MAX_EPISODES,
) -> list[str]:
    """topic に紐付く episodes を古い順に取得して整形 (新しい側を優先)."""
    rows = conn.execute(
        "SELECT role, content FROM episodes WHERE topic_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (topic_id, max_episodes),
    ).fetchall()
    rows = list(reversed(rows))  # 古い順に並べ直す
    return [f"[{r[0]}] {r[1]}" for r in rows]


def generate_topic_summary(
    conn: sqlite3.Connection,
    topic_id: str,
    client: LLMClient,
    model: str = TOPIC_SUMMARY_MODEL,
) -> tuple[str | None, str | None]:
    """topic を 1 LLM コールで要約. (title, summary) を返す.

    LLM 失敗時は (None, None). 呼出側が UPDATE をスキップする想定.
    """
    episode_lines = fetch_topic_episodes(conn, topic_id)
    if not episode_lines:
        return None, None
    prompt = build_prompt(episode_lines)
    try:
        response = client.generate(model, prompt, num_ctx=TOPIC_SUMMARY_NUM_CTX)
    except Exception:
        return None, None
    return parse_summary(response)


def update_topic_summary(
    conn: sqlite3.Connection, topic_id: str,
    title: str | None, summary: str | None,
) -> None:
    """生成した title/summary を topics に書き込む. None は無視."""
    if not title and not summary:
        return
    sets = []
    params: list = []
    if title:
        sets.append("title = ?")
        params.append(title)
    if summary:
        sets.append("summary = ?")
        params.append(summary)
    sets.append("last_active_at = datetime('now', '+9 hours')")
    params.append(topic_id)
    conn.execute(
        f"UPDATE topics SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    conn.commit()


def summarize_topic(
    conn: sqlite3.Connection, topic_id: str, client: LLMClient,
) -> bool:
    """1 topic を要約 → DB UPDATE. 成功時 True."""
    title, summary = generate_topic_summary(conn, topic_id, client)
    if not title and not summary:
        return False
    update_topic_summary(conn, topic_id, title, summary)
    return True


def find_topic_needing_summary(conn: sqlite3.Connection) -> str | None:
    """summary が空の topic を 1 件返す (古い順). 無ければ None."""
    row = conn.execute(
        "SELECT id FROM topics WHERE (summary IS NULL OR summary = '') "
        "ORDER BY created_at LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def find_topic_for_session(conn: sqlite3.Connection, session_id: str) -> str | None:
    """session に紐付いた現在の topic_id を返す.

    優先順位:
      1. meta override (continue_topic 経由)
      2. 直近 episode の topic_id (fallback for a session where override wasn't set)
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        (f"topic_for_session_{session_id}",),
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT topic_id FROM episodes WHERE session_id = ? "
        "AND topic_id IS NOT NULL ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row[0] if row else None
