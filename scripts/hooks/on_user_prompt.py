"""UserPromptSubmit hook 同期処理 (0.8.0 — Cozo 単独経路).

実行内容:
1. 機密チェック (検出時は raw 保存 / recall すべてスキップ、stderr 警告)
2. 反省モード state 取得 + 怒気検知 (instruction 注入 + lesson 浮上)
3. boot 層 dirty 再注入 (Cozo)
4. user 発話を episode に raw 保存 (Cozo)
5. topic 同定 (生きてる話題箱) + full recall (Cozo)
6. additionalContext 出力
7. write LLM を detach 起動 (Cozo episode_id を渡す)

fail-open: recall / write どこで失敗しても raw 保存は守られ Claude Code 本体は進む.
"""
from __future__ import annotations

import json
import os
import sys

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

    # Cozo path 解決 (.cozo.db 不在なら何もせず exit, init 未実行扱い).
    from scripts.db_cozo.wire import cozo_db_path_for, cozo_db_present
    if not cozo_db_present(db_path):
        sys.stderr.write(
            "[persona-memory] Cozo DB が見つかりません. "
            "/persona-memory:init もしくは /persona-memory:upgrade を実行してください.\n"
        )
        return 0
    cozo_path = cozo_db_path_for(db_path)

    additional_context = ""
    episode_id: int | None = None

    try:
        from scripts.db_cozo.connection import init_db
        from scripts.db_cozo.repo import save_episode
        client = init_db(cozo_path)

        # ── 反省モード state 取得 + 怒気検知 (0.7.7) ──
        reflection_block = ""
        lesson_prompt_block = ""
        try:
            from scripts.reflection.detect import detect_anger
            from scripts.reflection.instruction import (
                format_continue_instruction, format_enter_instruction,
            )
            from scripts.reflection.lesson import (
                format_lesson_block_for_prompt, match_lessons_for_prompt,
            )
            from scripts.reflection.state import (
                clear as _r_clear, enter as _r_enter,
                get_state as _r_get, increment_turn as _r_inc,
                should_force_clear as _r_force,
            )
            rstate = _r_get(client)
            angry, phrase = detect_anger(prompt, OllamaClient())
            if angry:
                _r_enter(client, episode_id=0, anger_phrase=phrase)
                reflection_block = format_enter_instruction(phrase)
            elif rstate.active:
                turn = _r_inc(client)
                if _r_force(client):
                    _r_clear(client)
                else:
                    reflection_block = format_continue_instruction(turn)
            try:
                lesson_matches = match_lessons_for_prompt(client, prompt)
                lesson_prompt_block = format_lesson_block_for_prompt(
                    lesson_matches,
                )
            except Exception as e:
                sys.stderr.write(
                    f"[persona-memory] lesson match failed: {e}\n",
                )
        except Exception as e:
            sys.stderr.write(
                f"[persona-memory] reflection wire failed: {e}\n",
            )

        # ── boot 層 dirty 再注入 (Cozo) ──
        boot_section = ""
        try:
            from scripts.boot.inject import format_boot_facts
            from scripts.db_cozo.fact_persist import (
                clear_boot_dirty, fetch_boot_facts, is_boot_dirty,
            )
            if is_boot_dirty(client):
                facts = fetch_boot_facts(client)
                if facts:
                    boot_section = format_boot_facts(facts)
                    clear_boot_dirty(client)
        except Exception as e:
            sys.stderr.write(f"[persona-memory] boot inject failed: {e}\n")

        # ── topic 同定 (= 生きてる話題箱) ──
        try:
            if os.environ.get("PERSONA_TOPIC_DISABLE", "").strip() != "1":
                from scripts.db_cozo.wire import maybe_cozo_identify_topic
                maybe_cozo_identify_topic(
                    db_path, session_id, prompt, role="user",
                )
        except Exception as e:
            sys.stderr.write(f"[persona-memory] topic identify failed: {e}\n")

        # ── user 発話を episode に raw 保存 ──
        try:
            episode_id = save_episode(
                client, role="user", content=prompt, session_id=session_id,
            )
        except Exception as e:
            sys.stderr.write(f"[persona-memory] save_episode failed: {e}\n")

        # ── full recall ──
        cozo_section = ""
        try:
            if os.environ.get("PERSONA_RECALL_DISABLE", "").strip() != "1":
                from scripts.db_cozo.wire import maybe_cozo_full_recall
                cozo_section = maybe_cozo_full_recall(db_path, prompt)
        except Exception as e:
            sys.stderr.write(f"[persona-memory] full recall failed: {e}\n")

        # 反省モード / lesson ブロックを最上位に置く.
        additional_context = "\n\n".join(
            s for s in (
                reflection_block, lesson_prompt_block,
                cozo_section, boot_section,
            ) if s
        )
    except Exception as e:
        sys.stderr.write(f"[persona-memory] hook failed: {e}\n")
        return 0

    # write LLM を detach 起動 (Cozo episode_id を渡す)
    if episode_id is not None:
        spawn_write([episode_id])

    if additional_context:
        _emit_additional_context(additional_context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
