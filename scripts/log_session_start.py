#!/usr/bin/env python3
"""Append a 'session-started' episode to the persona DB.

Called by .claude/hooks/on-session-start.sh AFTER it prints the inject text.
Records when the session began and a one-line summary of what context was
loaded, so future sessions can see "last session started at T, was primed
with topics X / Y / Z" without depending on the per-turn auto_persist hook.

Reads the inject summary from stdin (single line). Fail-open on any error.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from server import db, embedding  # noqa: E402
except Exception:
    sys.exit(0)


async def main_async() -> None:
    summary = sys.stdin.read().strip()
    if not summary:
        sys.exit(0)
    session_id = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get(
        "SESSION_ID"
    ) or "session-start"
    try:
        ep_id = db.append_episode(
            session_id=session_id[:64],
            role="system",
            content=f"SessionStart\n\n{summary}",
            summary=summary,
        )
    except Exception:
        return
    try:
        vec = await embedding.embed_text(summary)
        db.write_episode_embedding(ep_id, embedding.pack_embedding(vec))
    except Exception:
        pass


def main() -> None:
    try:
        asyncio.run(main_async())
    except Exception:
        pass


if __name__ == "__main__":
    main()
