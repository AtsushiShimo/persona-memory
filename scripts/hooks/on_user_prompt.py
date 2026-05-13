"""UserPromptSubmit hook 同期処理 (phase 4 範囲).

実行内容:
1. 機密チェック (検出時は raw 保存 / recall すべてスキップ、stderr 警告)
2. user 発話を episodes に raw 保存 (zero-loss)
3. recall: ローカル LLM がキーワード抽出 → DB 検索 → additionalContext 出力 ← phase 4 追加
4. write LLM を detach 起動 (saved episode_id を渡す)

fail-open: recall / write どこで失敗しても raw 保存は守られ Claude Code 本体は進む。
"""
from __future__ import annotations

import json
import os
import sys

from scripts.db.connection import connect
from scripts.db.repo import save_episode
from scripts.hooks.spawn import spawn_write
from scripts.secrets.detect import detect_secrets, warning_message
from scripts.shared.env import get_db_path, get_session_id_from_payload
from scripts.shared.ollama import OllamaClient


def _emit_additional_context(text: str) -> None:
    if not text:
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


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
        return 2  # block

    db_path = get_db_path()
    if db_path is None:
        return 0

    session_id = get_session_id_from_payload(payload)
    additional_context = ""
    try:
        conn = connect(db_path)
        try:
            episode_id = save_episode(conn, role="user", content=prompt, session_id=session_id)

            # ── boot 層 dirty 再注入 (§8.1) ──
            from scripts.boot.inject import (
                clear_dirty, fetch_boot_facts, format_boot_facts, is_dirty,
            )
            boot_section = ""
            if is_dirty(conn):
                boot_section = format_boot_facts(fetch_boot_facts(conn))
                clear_dirty(conn)

            # ── recall: 失敗しても sys.stderr に残して素通し ──
            recall_section = ""
            if os.environ.get("PERSONA_RECALL_DISABLE") != "1":
                try:
                    from scripts.recall.run import recall
                    recall_section = recall(
                        conn, prompt, OllamaClient(),
                        session_id=session_id,
                        source_episode_id=episode_id,
                    )
                except Exception as e:
                    sys.stderr.write(f"[persona-memory] recall failed: {e}\n")

            # ── Cozo 経路 (流れ再構築): .cozo.db が存在する時だけ追加 ──
            cozo_section = ""
            try:
                from scripts.db_cozo.wire import (
                    maybe_cozo_recall_block, maybe_cozo_save_episode,
                )
                # Cozo 側にも raw 保存 (新規発話の流れに乗せる)
                maybe_cozo_save_episode(
                    db_path, role="user", content=prompt, session_id=session_id,
                )
                cozo_section = maybe_cozo_recall_block(db_path, prompt)
            except Exception as e:
                sys.stderr.write(f"[persona-memory] cozo wire failed: {e}\n")

            additional_context = "\n\n".join(
                s for s in (cozo_section, boot_section, recall_section) if s
            )
        finally:
            conn.close()
    except Exception as e:
        sys.stderr.write(f"[persona-memory] raw save failed: {e}\n")
        return 0

    # write LLM を detach 起動
    spawn_write([episode_id])

    if additional_context:
        _emit_additional_context(additional_context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
