"""SessionEnd hook 同期処理.

session 全体の summary を Claude エスカレーションで生成し、context fact
として永続化する。長時間の議論内容が単発抽出で取りこぼされる問題を補う。

完全 detach: hook は即時 exit、heavy work (Claude API 呼出 + DB write +
embedding) は別プロセスで継続。
"""
from __future__ import annotations

import json
import sys

from scripts.db.connection import connect
from scripts.shared.env import get_db_path, get_session_id_from_payload
from scripts.shared.ollama import OllamaClient
from scripts.summary.session import generate_session_summary


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    db_path = get_db_path()
    if db_path is None:
        return 0

    session_id = get_session_id_from_payload(payload)
    if not session_id or session_id == "unknown":
        return 0

    try:
        conn = connect(db_path)
    except Exception as e:
        sys.stderr.write(f"[persona-memory] db open failed: {e}\n")
        return 0

    try:
        generate_session_summary(conn, session_id, OllamaClient())
    except Exception as e:
        sys.stderr.write(f"[persona-memory] session summary failed: {e}\n")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
