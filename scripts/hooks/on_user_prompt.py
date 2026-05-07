"""UserPromptSubmit hook 同期処理 (phase 3 範囲).

実行内容:
1. 機密チェック (検出時は raw 保存 / recall すべてスキップ、stderr 警告)
2. user 発話を episodes に raw 保存 (zero-loss)
3. write LLM を detach 起動 (saved episode_id を渡す) ← phase 3 追加
4. recall は phase 4 で追加 (現状は何も注入しない)

fail-open: 例外 / 未初期化 DB は exit 0 で素通し。Claude Code 本体を止めない。
"""
from __future__ import annotations

import json
import sys

from scripts.db.connection import connect
from scripts.db.repo import save_episode
from scripts.hooks.spawn import spawn_write
from scripts.secrets.detect import detect_secrets, warning_message
from scripts.shared.env import get_db_path, get_session_id_from_payload


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return 0

    found = detect_secrets(prompt)
    if found:
        sys.stderr.write(warning_message(found))
        return 2  # block: prompt は Anthropic にも送らない

    db_path = get_db_path()
    if db_path is None:
        return 0  # 未初期化なら no-op

    session_id = get_session_id_from_payload(payload)
    try:
        conn = connect(db_path)
        try:
            episode_id = save_episode(conn, role="user", content=prompt, session_id=session_id)
        finally:
            conn.close()
    except Exception as e:
        sys.stderr.write(f"[persona-memory] raw save failed: {e}\n")
        return 0  # fail-open

    # write LLM を detach 起動 (Claude Code 本体は待たない)
    spawn_write([episode_id])
    return 0


if __name__ == "__main__":
    sys.exit(main())
