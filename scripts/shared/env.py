"""環境変数からの設定解決。fail-open: 未初期化なら None を返し、呼び出し側が no-op する。"""
from __future__ import annotations

import os
from pathlib import Path


def get_db_path() -> Path | None:
    """PERSONA_MEMORY_DB env var から DB パスを取得。未設定なら None。

    .claude/hooks/*.sh は load_persona_env.sh をソースしてからこの env を立てる。
    DB が存在しない場合も None 扱い (fail-open)。
    """
    raw = os.environ.get("PERSONA_MEMORY_DB", "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.exists():
        return None
    return p


def get_session_id_from_payload(payload: dict) -> str:
    """Claude Code hook payload から session_id を取得。最大 64 文字。"""
    sid = (payload.get("session_id") or "").strip()
    return sid[:64] if sid else "unknown"
