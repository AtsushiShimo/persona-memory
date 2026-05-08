"""session 全体の要約を Claude エスカレーションで生成し、context fact として永続化.

write LLM (gemma3:4b) は単発発話単位での fact 抽出しかできず、長時間の議論
(例: 30 ターンに渡る Phase 分け、課金モデル選定、実装優先度決定) を捕捉できない。
SessionEnd hook で session 全体を Claude に要約させ、context category の
fact として保存する。これで:
- 議論の決定事項が構造化記憶に残る
- 後の recall で『前回の議論』 系クエリと意味的に近い fact がヒットする
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from scripts.escalate.claude_p import invoke_claude
from scripts.shared.embedding import pack
from scripts.shared.ollama import LLMClient

JST = timezone(timedelta(hours=9))
EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")

# 要約を生成する閾値: session 内の user/assistant ターン数がこれ未満なら skip
MIN_TURNS_FOR_SUMMARY = int(os.environ.get("PERSONA_SUMMARY_MIN_TURNS", "6"))


def fetch_session_turns(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, role, content, timestamp FROM episodes "
        "WHERE session_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    return [
        {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
        for r in rows
    ]


def build_summary_prompt(turns: list[dict]) -> str:
    transcript = "\n\n".join(
        f"[{t['timestamp']}] [{t['role']}]\n{t['content']}" for t in turns
    )
    return f"""\
以下は 1 セッションの会話ログです。長期記憶に残すべき **議論の流れ・決定事項・進行中のトピック** を 3-8 行の自然な日本語で要約してください。

要約に含めるもの:
- 議論された主題・トピックの遷移
- 確定した決定 (例: 採用された方針、保留にした項目、廃案にした案)
- 進行中で未解決のもの
- ユーザーが新しく共有した個人情報・好み・属性 (まだ単発で fact 化されていれば不要)

要約に含めないもの:
- ファイルパス・コマンド・ツール名等の技術メタ情報
- アシスタントの口調・人格 (中立な 3 人称、または無主語で客観的に書く)
- 「申し訳ございません」「ありがとうございます」 等の挨拶・社交辞令

会話ログ:
---
{transcript}
---

要約 (3-8 行、説明・前置き・コードフェンス禁止):"""


def upsert_session_summary_fact(
    conn: sqlite3.Connection,
    session_id: str,
    summary: str,
    embed_client: LLMClient,
) -> int | None:
    """session_id に対応する context/session_summary_<sid> fact を upsert.

    既存があれば supersede + 新版 insert (履歴チェーン)、無ければ新規 insert。
    """
    if not summary.strip():
        return None
    short_id = session_id[:12] if session_id else "unknown"
    key = f"session_summary_{short_id}"
    importance = 7

    # supersedes チェーンを張る
    row = conn.execute(
        "SELECT id FROM facts WHERE category='context' AND key=? AND status='active'",
        (key,),
    ).fetchone()
    old_id = row[0] if row else None

    if old_id is not None:
        conn.execute(
            "UPDATE facts SET status='superseded', "
            "  updated_at=datetime('now', '+9 hours') WHERE id=?",
            (old_id,),
        )

    cur = conn.execute(
        "INSERT INTO facts(category, key, value, importance, source, supersedes) "
        "VALUES ('context', ?, ?, ?, 'session_summary', ?)",
        (key, summary, importance, old_id),
    )
    new_id = cur.lastrowid

    if old_id is not None:
        conn.execute(
            "UPDATE facts SET superseded_by=? WHERE id=?",
            (new_id, old_id),
        )

    # embedding (recall でヒットさせるため)
    try:
        vec = embed_client.embed(EMBED_MODEL, summary)
        if vec:
            conn.execute(
                "INSERT OR REPLACE INTO fact_embeddings(fact_id, embedding) "
                "VALUES (?, ?)",
                (new_id, pack(vec)),
            )
    except Exception as e:
        sys.stderr.write(
            f"[persona-memory] session_summary embedding failed: {e}\n"
        )

    conn.commit()
    return new_id


def generate_session_summary(
    conn: sqlite3.Connection,
    session_id: str,
    embed_client: LLMClient,
) -> int | None:
    """1 session の要約 fact を生成 + 保存する。戻り値: 新 fact id (or None).

    閾値未満のターン数なら skip。Claude エスカレーション失敗時も skip。
    """
    if not session_id:
        return None
    turns = fetch_session_turns(conn, session_id)
    if len(turns) < MIN_TURNS_FOR_SUMMARY:
        return None

    prompt = build_summary_prompt(turns)
    result = invoke_claude(prompt)
    if not result.success or not result.text.strip():
        sys.stderr.write(
            f"[persona-memory] session summary skipped "
            f"(claude error: {result.error[:80] if result.error else 'empty'})\n"
        )
        return None

    return upsert_session_summary_fact(conn, session_id, result.text.strip(), embed_client)
