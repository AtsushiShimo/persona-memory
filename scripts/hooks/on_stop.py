"""Stop hook 同期処理 (0.8.0 — Cozo 単独経路).

実行内容:
1. transcript_path から新規ターンを抽出 (last_idx を Cozo meta に記録)
2. 機密チェック (検出ターンは保存スキップ + 警告)
3. user / assistant ターンを episode に raw 保存 (Cozo)
4. write LLM を detach 起動 (saved episode_ids を渡す)

fail-open: 例外 / 未初期化 DB は exit 0 で素通し.
個別 episode の保存失敗で全件取りこぼさないよう try/except で囲む.
"""
from __future__ import annotations

import json
import sys

from scripts.hooks.spawn import spawn_write
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

    from scripts.db_cozo.wire import cozo_db_path_for, cozo_db_present
    if not cozo_db_present(db_path):
        sys.stderr.write(
            "[persona-memory] Cozo DB が見つかりません (Stop hook). "
            "/persona-memory:upgrade を実行してください.\n"
        )
        return 0
    cozo_path = cozo_db_path_for(db_path)

    session_id = get_session_id_from_payload(payload)
    messages = read_transcript(transcript_path)
    if not messages:
        return 0

    saved_ids: list[int] = []
    try:
        from scripts.db_cozo.connection import init_db
        from scripts.db_cozo.repo import (
            get_meta, save_episode, set_meta,
        )
        client = init_db(cozo_path)

        meta_key = f"raw_save_last_idx:{session_id}"
        last_raw = get_meta(client, meta_key)
        try:
            last_idx = int(last_raw) if last_raw is not None else 0
        except (ValueError, TypeError):
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
                continue
            try:
                eid = save_episode(
                    client, role=role, content=content,
                    session_id=session_id,
                )
                saved_ids.append(eid)
            except Exception as e:
                # 個別 episode の保存失敗で全件取りこぼさない.
                sys.stderr.write(
                    f"[persona-memory] save_episode (Stop) failed: {e}\n"
                )

        set_meta(client, meta_key, str(len(messages)))
    except Exception as e:
        sys.stderr.write(f"[persona-memory] Stop hook failed: {e}\n")
        return 0

    if saved_ids:
        spawn_write(saved_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
