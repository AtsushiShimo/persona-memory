"""プロセス全体のハードタイムアウト (0.8.7).

httpx より下のレイヤ (ソケット, Cozo init, fcntl 等) で hang した場合の最後の保険.
`signal.alarm()` で SIGALRM を発火させ, ハンドラで stderr に痕跡を残して exit する.

無効化: timeout <= 0 で no-op.
"""
from __future__ import annotations

import os
import signal
import sys


def _handler(signum, frame) -> None:
    sys.stderr.write(
        "[persona-memory] watchdog fired (process exceeded "
        f"{_armed_for[0]}s budget) - terminating.\n",
    )
    sys.stderr.flush()
    # exit code 124 = GNU timeout 流儀 (タイムアウト超過).
    os._exit(124)


# 直近 arm された秒数を handler から参照するための保管箱.
_armed_for: list[int] = [0]


def arm_watchdog(timeout_seconds: int) -> bool:
    """SIGALRM を `timeout_seconds` 秒後に発火する設定をする.

    Returns:
        True: alarm を実際に立てた.
        False: 無効化 (timeout <= 0) で何もしなかった.
    """
    t = int(timeout_seconds)
    if t <= 0:
        return False
    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(t)
    _armed_for[0] = t
    return True


def disarm_watchdog() -> None:
    """立てた alarm を取り消す."""
    signal.alarm(0)
    _armed_for[0] = 0
