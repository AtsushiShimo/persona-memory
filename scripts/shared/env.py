"""環境変数からの設定解決。fail-open: 未初期化なら None を返し、呼び出し側が no-op する。"""
from __future__ import annotations

import os
from pathlib import Path


def get_db_path() -> Path | None:
    """PERSONA_MEMORY_DB env var から Cozo DB パスを取得。未設定 / 不在なら None。

    `.claude/hooks/*.sh` は `load_persona_env.sh` を source してからこの env を立てる.
    env が旧 SQLite path (`<persona>.db`) を指す古い config.env も path-only で
    `<persona>.cozo.db` に振り直して存在判定する (= /persona-memory:upgrade で
    in-place migrate されるまでの後方互換).

    Cozo DB が存在しない場合は None 扱い (fail-open). 呼出側 hook は no-op する.
    """
    raw = os.environ.get("PERSONA_MEMORY_DB", "").strip()
    if not raw:
        return None
    p = Path(raw)
    # 旧 SQLite path を受けたら `.cozo.db` に振り直す.
    if p.suffix == ".db" and not p.name.endswith(".cozo.db"):
        p = p.with_suffix(".cozo.db")
    if not p.exists():
        return None
    return p


def get_session_id_from_payload(payload: dict) -> str:
    """Claude Code hook payload から session_id を取得。最大 64 文字。"""
    sid = (payload.get("session_id") or "").strip()
    return sid[:64] if sid else "unknown"
