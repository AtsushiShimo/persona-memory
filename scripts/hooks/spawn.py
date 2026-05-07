"""detach 子プロセスとして write LLM を起動するヘルパ。

PERSONA_WRITE_DISABLE=1 でテスト時に無効化できる (= 実 LLM を呼ばない)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def spawn_write(episode_ids: list[int]) -> None:
    if os.environ.get("PERSONA_WRITE_DISABLE") == "1":
        return
    if not episode_ids:
        return

    payload = json.dumps({"episode_ids": episode_ids}).encode()
    try:
        p = subprocess.Popen(
            [sys.executable, "-m", "scripts.write.run"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        # write payload then close stdin to release the child immediately
        if p.stdin:
            p.stdin.write(payload)
            p.stdin.close()
    except Exception:
        # fail-open: 子プロセス起動失敗でも本処理 (raw 保存) は完了済み
        pass
