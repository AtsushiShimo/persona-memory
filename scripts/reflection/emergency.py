"""反省モードの緊急停止 / 復帰 (0.8.5).

slash command `/persona-memory:reflection-off` から呼ばれる. env 変数では
セッション再起動が必要で緊急用途に合わないため、 Cozo の reflection_state
relation に persist された detection_enabled フラグを動的に切替える経路.

使い方:
  python -m scripts.reflection.emergency off  # 検知 disable + 現状 clear
  python -m scripts.reflection.emergency on   # 検知 enable
  python -m scripts.reflection.emergency status  # 現在の状態 JSON

DB path は環境変数 PERSONA_MEMORY_DB から取得 (slash command 側で渡す).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _resolve_db() -> Path:
    env = os.environ.get("PERSONA_MEMORY_DB", "").strip()
    if not env:
        sys.stderr.write(
            "ERROR: PERSONA_MEMORY_DB が未設定です. "
            "slash command 側で渡してください.\n",
        )
        sys.exit(2)
    return Path(env)


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: emergency {on|off|status}\n")
        return 2
    action = sys.argv[1].strip().lower()
    db_path = _resolve_db()

    from scripts.db_cozo.connection import init_db
    from scripts.db_cozo.wire import cozo_db_path_for
    from scripts.reflection.state import (
        clear, get_state, is_detection_enabled, set_detection_enabled,
    )

    cozo_path = cozo_db_path_for(db_path)
    if not cozo_path.exists():
        sys.stderr.write(f"ERROR: Cozo DB が見つかりません: {cozo_path}\n")
        return 1
    client = init_db(cozo_path)

    if action == "off":
        cleared = get_state(client).active
        clear(client)
        set_detection_enabled(client, False)
        print(json.dumps({
            "ok": True, "action": "off", "detection_enabled": False,
            "reflection_state_cleared": cleared,
        }, ensure_ascii=False))
        return 0
    if action == "on":
        set_detection_enabled(client, True)
        print(json.dumps({
            "ok": True, "action": "on", "detection_enabled": True,
        }, ensure_ascii=False))
        return 0
    if action == "status":
        st = get_state(client)
        print(json.dumps({
            "ok": True,
            "detection_enabled": is_detection_enabled(client),
            "reflection_active": st.active,
            "turn_count": st.turn_count,
            "started_at": st.started_at,
            "anger_phrase": st.anger_phrase,
        }, ensure_ascii=False))
        return 0
    sys.stderr.write(f"ERROR: unknown action {action!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
