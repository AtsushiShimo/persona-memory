#!/usr/bin/env python3
"""Generate a one-line session-start greeting from recent episode summaries.

Reads lines from stdin (format: "DATETIME: SUMMARY"),
calls the judge model, and prints a single greeting line.
Fail-open: any error -> silent exit with no output.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import db  # noqa: E402

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
JUDGE_MODEL = os.environ.get("PERSONA_JUDGE_MODEL", "gemma3:4b")
TIMEOUT = 12.0


async def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        return

    # Resolve the persona's display name from DB so the greeting is
    # personalized without hard-coding any specific name in source.
    assistant_name = db.get_persona_name(default="アシスタント")

    prompt = (
        f"あなたは「{assistant_name}」という AI アシスタントです。敬語を使います。\n"
        "以下は直近セッションの議論要約です:\n\n"
        f"{raw}\n\n"
        "新しいセッションの最初にユーザーへ送る一言を生成してください。\n"
        "条件:\n"
        "- 直近の議論から主要なトピックを 2〜5 語で特定し、"
        "「〈そのトピック名〉の話の続きから始めますか？」という形で質問する\n"
        "  例: 「デフォルトモデルの変更の話の続きから始めますか？」\n"
        "- 続きがない場合は短い挨拶のみ\n"
        "- 1〜2 文以内\n"
        "- 「こんにちは」などの挨拶フレーズは不要、本題から入る\n"
        "- 余計な記号・前置き一切不要。発話テキストのみ出力"
    )

    try:
        import httpx  # noqa: PLC0415

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": JUDGE_MODEL, "prompt": prompt, "stream": False},
            )
            r.raise_for_status()
            text = (r.json().get("response") or "").strip().strip('"').strip("「」")
            if text:
                print(text)
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
