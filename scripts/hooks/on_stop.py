"""Stop hook 同期処理 (phase 2 範囲).

実行内容:
1. transcript_path から新規 assistant ターンを抽出 (last_idx を meta に記録)
2. 機密チェック (検出ターンは保存スキップ + 警告)
3. assistant 応答を episodes に raw 保存 (zero-loss)
4. write LLM 起動は phase 3 で追加 (現状なし)
5. lint 起動は post-MVP

fail-open: 例外 / 未初期化 DB は exit 0 で素通し。
"""
from __future__ import annotations

import json
import sys

from scripts.db.connection import connect
from scripts.db.repo import get_meta, save_episode, set_meta
from scripts.secrets.detect import detect_secrets, warning_message
from scripts.shared.env import get_db_path, get_session_id_from_payload
from scripts.shared.transcript import read_transcript


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    transcript_path = payload.get("transcript_path") or ""
    if not transcript_path:
        return 0

    db_path = get_db_path()
    if db_path is None:
        return 0

    session_id = get_session_id_from_payload(payload)
    messages = read_transcript(transcript_path)
    if not messages:
        return 0

    try:
        conn = connect(db_path)
    except Exception as e:
        sys.stderr.write(f"[persona-memory] db open failed: {e}\n")
        return 0

    try:
        meta_key = f"raw_save_last_idx:{session_id}"
        last_raw = get_meta(conn, meta_key)
        try:
            last_idx = int(last_raw) if last_raw is not None else 0
        except ValueError:
            last_idx = 0

        new_msgs = messages[last_idx:]
        if not new_msgs:
            return 0

        for msg in new_msgs:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role not in ("user", "assistant") or not content.strip():
                continue
            found = detect_secrets(content)
            if found:
                sys.stderr.write(warning_message(found))
                continue  # この turn だけスキップ
            save_episode(conn, role=role, content=content, session_id=session_id)

        set_meta(conn, meta_key, str(len(messages)))
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
