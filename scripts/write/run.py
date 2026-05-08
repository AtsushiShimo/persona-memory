"""write 主エントリ。detached child で起動され、新規 episode を fact 化する。

フロー:
1. stdin から JSON 受信: {"episode_ids": [42, 43], "buffer_n": 3}
2. 各 episode について:
   a. 直前 BUFFER_N 発話を episodes から取得
   b. write LLM で fact 抽出
   c. 各 candidate について:
      - 候補 value を embedding 化
      - find_match (key → embedding) で既存検索
      - apply_candidate (補強 / 変更 / 挿入)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Iterable

from scripts.db.connection import connect
from scripts.db.repo import get_meta, set_meta
from scripts.escalate.claude_p import invoke_claude, log_escalation
from scripts.escalate.decide import estimate_tokens, should_escalate
from scripts.shared.embedding import pack
from scripts.shared.env import get_db_path
from scripts.shared.ollama import LLMClient, OllamaClient
from scripts.write.extract import (
    FactCandidate,
    WRITE_MODEL,
    build_prompt,
    extract_facts,
    parse_response,
)
from scripts.write.persist import apply_candidate
from scripts.write.similarity import find_match

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
DEFAULT_BUFFER_N = int(os.environ.get("PERSONA_BUFFER_N", "3"))
PROCESSED_META_KEY = "write_processed_max_id"


def get_processed_max_id(conn: sqlite3.Connection) -> int:
    raw = get_meta(conn, PROCESSED_META_KEY)
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0


def mark_processed(conn: sqlite3.Connection, episode_id: int) -> None:
    cur = get_processed_max_id(conn)
    if episode_id > cur:
        set_meta(conn, PROCESSED_META_KEY, str(episode_id))


def fetch_unprocessed_episode_ids(conn: sqlite3.Connection) -> list[int]:
    """write_processed_max_id 以降の episode id を昇順で返す。"""
    cur = get_processed_max_id(conn)
    rows = conn.execute(
        "SELECT id FROM episodes WHERE id > ? ORDER BY id",
        (cur,),
    ).fetchall()
    return [r[0] for r in rows]


def fetch_episode(conn: sqlite3.Connection, episode_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, role, content, session_id FROM episodes WHERE id = ?",
        (episode_id,),
    ).fetchone()
    if not row:
        return None
    return {"id": row[0], "role": row[1], "content": row[2], "session_id": row[3]}


def fetch_buffer(conn: sqlite3.Connection, before_id: int, n: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM episodes WHERE id < ? ORDER BY id DESC LIMIT ?",
        (before_id, n),
    ).fetchall()
    # 古い順に並び替え
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def _extract_via_claude(role: str, content: str, buffer: list[dict]) -> list[FactCandidate]:
    """長文時は Claude に抽出を任せる (§3.1 long_input エスカレーション)."""
    prompt = build_prompt(role, content, buffer)
    r = invoke_claude(prompt)
    if not r.success:
        return []
    return parse_response(r.text)


def _embed_episode(
    conn: sqlite3.Connection,
    episode_id: int,
    content: str,
    client: LLMClient,
    embed_model: str,
) -> None:
    """episode 全文を embedding 化して episode_embeddings に格納.

    既に entry がある場合は INSERT OR REPLACE で更新。
    失敗時は何もしない (recall 側で当該 episode が引けないだけで他には影響なし)。
    """
    if not content or not content.strip():
        return
    try:
        vec = client.embed(embed_model, content)
        if not vec:
            return
        conn.execute(
            "INSERT OR REPLACE INTO episode_embeddings(episode_id, embedding) VALUES (?, ?)",
            (episode_id, pack(vec)),
        )
        conn.commit()
    except Exception as e:
        sys.stderr.write(f"[persona-memory] episode embedding failed (id={episode_id}): {e}\n")


def process_episode(
    conn: sqlite3.Connection,
    episode_id: int,
    buffer_n: int,
    client: LLMClient,
    write_model: str = WRITE_MODEL,
    embed_model: str = EMBED_MODEL,
) -> list[tuple[FactCandidate, str]]:
    """1 episode を処理し、(candidate, action) のリストを返す。

    副作用: 当該 episode の content を embedding 化して episode_embeddings に格納
    (recall 時のベクトル検索の対象になる)。
    """
    episode = fetch_episode(conn, episode_id)
    if not episode:
        return []
    buffer = fetch_buffer(conn, episode_id, buffer_n)

    # episode 全文を embedding 化 (recall 時のベクトル検索のため)
    _embed_episode(conn, episode_id, episode["content"], client, embed_model)

    # 入力サイズで long_input エスカレーション判定
    full_input = episode["content"] + "\n" + "\n".join(b.get("content", "") for b in buffer)
    reason = should_escalate(input_text=full_input)

    if reason == "long_input":
        candidates = _extract_via_claude(episode["role"], episode["content"], buffer)
        log_escalation(
            conn, reason="long_input", caller="write",
            input_size=estimate_tokens(full_input),
            outcome=f"extracted {len(candidates)} facts",
        )
    else:
        candidates = extract_facts(
            role=episode["role"],
            content=episode["content"],
            buffer=buffer,
            client=client,
            model=write_model,
        )
    if not candidates:
        return []

    results: list[tuple[FactCandidate, str]] = []
    for cand in candidates:
        try:
            embedding = client.embed(embed_model, cand.value)
        except Exception:
            embedding = []
        match = find_match(conn, cand.category, cand.key, embedding)

        # importance >= 8 で既存と矛盾 (supersede 候補) なら裁定をログだけ残す。
        # phase 6 では Claude に裁定させずローカル判定に任せる (= heuristic 採用)。
        # uncertainty 判定の精緻化は post-MVP。
        if cand.importance >= 8 and match is not None:
            log_escalation(
                conn, reason="high_importance", caller="write",
                input_size=estimate_tokens(cand.value),
                outcome=f"local-decided overwrite of {match.category}/{match.key}",
            )

        action = apply_candidate(conn, cand, match, embedding, source="conversation")
        results.append((cand, action))
    return results


def run(episode_ids: Iterable[int], buffer_n: int = DEFAULT_BUFFER_N, client: LLMClient | None = None) -> int:
    db_path = get_db_path()
    if db_path is None:
        return 0
    cli = client or OllamaClient()
    conn = connect(db_path)
    try:
        for eid in episode_ids:
            try:
                process_episode(conn, eid, buffer_n, cli)
                mark_processed(conn, eid)
            except Exception as e:
                sys.stderr.write(f"[persona-memory] write process_episode {eid} failed: {e}\n")
                continue
    finally:
        conn.close()
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    episode_ids = payload.get("episode_ids") or []
    if not episode_ids:
        return 0
    buffer_n = int(payload.get("buffer_n", DEFAULT_BUFFER_N))
    return run(episode_ids, buffer_n)


if __name__ == "__main__":
    sys.exit(main())
