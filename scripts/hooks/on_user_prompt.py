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
            # Phase 5 (0.7.4): Cozo に fact があれば Cozo dirty を優先.
            # Cozo が空 (= upgrade-cozo 直後で write が未稼働) なら SQLite fallback.
            from scripts.boot.inject import (
                clear_dirty, fetch_boot_facts, format_boot_facts, is_dirty,
            )
            from scripts.db_cozo.wire import (
                cozo_db_present, cozo_db_path_for, maybe_cozo_full_recall,
                maybe_cozo_identify_topic, maybe_cozo_save_episode,
            )
            cozo_active = cozo_db_present(db_path)
            boot_section = ""
            cozo_boot_used = False
            if cozo_active:
                try:
                    from scripts.db_cozo.connection import init_db as _cozo_init_db
                    from scripts.db_cozo.fact_persist import (
                        clear_boot_dirty as _cozo_clear_boot_dirty,
                        fetch_boot_facts as _cozo_fetch_boot_facts,
                        is_boot_dirty as _cozo_is_boot_dirty,
                    )
                    _cclient = _cozo_init_db(cozo_db_path_for(db_path))
                    if _cozo_is_boot_dirty(_cclient):
                        cfacts = _cozo_fetch_boot_facts(_cclient)
                        if cfacts:
                            boot_section = format_boot_facts(cfacts)
                            _cozo_clear_boot_dirty(_cclient)
                            cozo_boot_used = True
                except Exception as e:
                    sys.stderr.write(f"[persona-memory] cozo boot failed: {e}\n")
            if not cozo_boot_used and is_dirty(conn):
                boot_section = format_boot_facts(fetch_boot_facts(conn))
                clear_dirty(conn)

            # ── Cozo 経路 (.cozo.db 存在時 → メイン recall として使う) ──
            cozo_section = ""
            try:
                # 0.7.6: 「生きてる話題箱」 方式で topic を同定 → active topic 切替.
                # 旧 maybe_cozo_topic_shift (active topic との 1 対 1 比較 +
                # cross-session merge) を完全に包摂. embedding で並列照合し、
                # 該当があれば summary を更新、 無ければ新 topic を作成.
                if cozo_active:
                    maybe_cozo_identify_topic(
                        db_path, session_id, prompt, role="user",
                    )
                # Cozo 側にも raw 保存 (新規発話を Cozo にも流す).
                # write LLM は当面 SQLite なので両方保存. 将来は Cozo 単独化予定.
                maybe_cozo_save_episode(
                    db_path, role="user", content=prompt, session_id=session_id,
                )
                if cozo_active:
                    cozo_section = maybe_cozo_full_recall(db_path, prompt)
            except Exception as e:
                sys.stderr.write(f"[persona-memory] cozo wire failed: {e}\n")

            # ── SQLite recall: Cozo 未移行 (.cozo.db 不在) の時のみ実行 ──
            recall_section = ""
            if not cozo_active and os.environ.get("PERSONA_RECALL_DISABLE") != "1":
                try:
                    from scripts.recall.run import recall
                    recall_section = recall(
                        conn, prompt, OllamaClient(),
                        session_id=session_id,
                        source_episode_id=episode_id,
                    )
                except Exception as e:
                    sys.stderr.write(f"[persona-memory] recall failed: {e}\n")

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
