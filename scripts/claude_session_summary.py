#!/usr/bin/env python3
"""Generate a high-quality session summary via Claude CLI (`claude -p`)
and persist it as a `role='system'` episode.

Hybrid design (rule/memory_save_policy):
- Write side stores raw turns (raw_dump 等) for long-term fidelity.
- This script adds an OPTIONAL Claude-quality summary at session boundaries
  for fast next-session handoff (matches claude-mem class polish).
- Per-utterance recall stays on Ollama (token-efficient).

Designed to run detached from PreCompact / SessionEnd hooks AFTER raw_dump
has already been saved. Failure here only loses the polished summary;
raw turns survive in DB regardless.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.persist_before_compact import (  # noqa: E402
    ASSISTANT_NAME,
    PER_MESSAGE_CAP,
    RECENT_TURNS,
    TOTAL_CAP,
    read_transcript,
)
from server import db, embedding  # noqa: E402

CLAUDE_BIN = os.environ.get("PERSONA_CLAUDE_BIN") or shutil.which("claude") or "claude"
CLAUDE_TIMEOUT = float(os.environ.get("PERSONA_CLAUDE_SUMMARY_TIMEOUT", "180.0"))


def build_prompt(messages: list[dict], kind: str) -> str:
    lines = [f"[{m['role']}] {m['content'][:PER_MESSAGE_CAP]}" for m in messages]
    convo = "\n".join(lines)
    if len(convo) > TOTAL_CAP:
        convo = convo[-TOTAL_CAP:]
    return (
        f"以下は人間 (User) と AI アシスタント "
        f"(Assistant, ペルソナ名: {ASSISTANT_NAME}) の会話抜粋です。\n"
        "次回別セッションで「最後に何をしていたか」を確実に思い出して再開できるよう、"
        "500 字以内の日本語で要約してください。\n\n"
        "**必ず含める**:\n"
        "- 決定事項・指示・好み・属性\n"
        "- アシスタントが実際に行った具体的作業 "
        "(編集ファイル / 実行コマンド / 修正点)\n"
        "- 進行中タスクの現状と次予定\n"
        "- 未解決の問題\n\n"
        "**正確性ルール (絶対遵守)**:\n"
        "- 修飾語・限定句・但し書きを省略しない "
        "(例: 「PM 含めて 8 人」 の『PM 含めて』、「Java は 5 年だが避けたい派」 の『が避けたい派』)\n"
        "- 数値はそのまま保持 (丸めない・概算化しない)\n"
        "- 否定・忌避の極性を反転しない\n"
        "- 訂正された古情報は混ぜない (最新の正解のみ)\n\n"
        "**禁止**: 会話に書かれていない情報の補完・推測。本文のみ出力 "
        "(前置き・後書き不要)。\n\n"
        f"会話 (kind={kind}):\n{convo}\n\n要約:"
    )


async def main_async() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    session_id = (payload.get("session_id") or "")[:64]
    transcript_path = payload.get("transcript_path") or ""
    kind = os.environ.get("PERSONA_SNAPSHOT_KIND", "PreCompact")

    if not transcript_path or not Path(transcript_path).exists():
        return
    messages = read_transcript(transcript_path)
    if not messages:
        return
    recent = messages[-RECENT_TURNS:]

    if not CLAUDE_BIN or not shutil.which(CLAUDE_BIN):
        sys.stderr.write(
            "[persona-memory] claude CLI not found — "
            "Claude session summary skipped (raw turns preserved)\n"
        )
        return

    prompt = build_prompt(recent, kind)
    # 子プロセス側で persona-memory hooks が再帰発火しないよう sentinel をセット
    env = os.environ.copy()
    env["PERSONA_SUMMARY_CHILD"] = "1"
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--permission-mode", "bypassPermissions"],
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            env=env,
        )
        summary = (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"[persona-memory] claude -p timed out after {CLAUDE_TIMEOUT}s "
            "for session summary (raw turns preserved)\n"
        )
        return
    except Exception as e:
        sys.stderr.write(
            f"[persona-memory] claude -p failed: {type(e).__name__} "
            "(raw turns preserved)\n"
        )
        return
    if not summary:
        return

    content = (
        f"{kind} snapshot via Claude "
        f"(turns_total={len(messages)}, turns_summarized={len(recent)})\n\n"
        f"{summary}"
    )
    try:
        ep_id = db.append_episode(
            session_id=session_id,
            role="system",
            content=content,
            summary=summary,
        )
    except Exception:
        return
    try:
        vec = await embedding.embed_text(summary)
        db.write_episode_embedding(ep_id, embedding.l2_normalize(vec))
    except Exception:
        pass


def main() -> None:
    try:
        asyncio.run(main_async())
    except Exception:
        pass


if __name__ == "__main__":
    main()
