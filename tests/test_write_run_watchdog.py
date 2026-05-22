"""write.run プロセス全体ウォッチドッグテスト (0.8.7).

httpx より下のレイヤ (ソケット, Cozo init 等) で hang した場合の最後の保険.
`signal.alarm()` で main() 全体に上限時間を設定し、 超過時は SIGALRM で
exit code 124 (= GNU timeout 流儀) で終了する.

※ 発火経路は subprocess 経由でテストする. 単体プロセスで watchdog を発火させると
pytest プロセス自体を `os._exit` で巻き込んでしまうため.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time


def test_arm_watchdog_disabled_by_zero():
    """timeout 0 は arm せず False を返す."""
    from scripts.shared.watchdog import arm_watchdog, disarm_watchdog
    assert arm_watchdog(0) is False
    disarm_watchdog()


def test_arm_watchdog_disabled_by_negative():
    """負数も arm せず False を返す."""
    from scripts.shared.watchdog import arm_watchdog, disarm_watchdog
    assert arm_watchdog(-1) is False
    disarm_watchdog()


def test_arm_watchdog_positive_arms_then_can_disarm():
    """正の timeout は arm 成功. テスト終了までに disarm すれば SIGALRM は飛ばない."""
    from scripts.shared.watchdog import arm_watchdog, disarm_watchdog
    armed = arm_watchdog(60)
    try:
        assert armed is True
    finally:
        disarm_watchdog()


def test_write_run_subprocess_watchdog_fires():
    """write.run を PERSONA_WRITE_WATCHDOG_SEC=2 で起動し stdin を hang させる.
    stdin timeout (30s) より早く watchdog (2s) が発火して非 0 終了.
    """
    env = os.environ.copy()
    env["PERSONA_WRITE_WATCHDOG_SEC"] = "2"
    env["PERSONA_WRITE_STDIN_TIMEOUT"] = "30"

    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.write.run"],
        stdin=subprocess.PIPE,  # 開くが書かない / 閉じない
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    t0 = time.time()
    try:
        rc = proc.wait(timeout=8.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError("watchdog did not fire within 8s")
    elapsed = time.time() - t0
    assert 1.0 < elapsed < 6.0, f"unexpected elapsed: {elapsed:.1f}s"
    assert rc != 0, "watchdog 発火時は非 0 終了であるべき"


def test_write_run_subprocess_watchdog_disabled_falls_back_to_stdin_timeout():
    """PERSONA_WRITE_WATCHDOG_SEC=0 で watchdog 無効. stdin timeout (短く設定) で死ぬ.

    回帰防止: watchdog disable 経路で write.run が永遠に動かないこと.
    """
    env = os.environ.copy()
    env["PERSONA_WRITE_WATCHDOG_SEC"] = "0"
    env["PERSONA_WRITE_STDIN_TIMEOUT"] = "2"

    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.write.run"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    t0 = time.time()
    try:
        rc = proc.wait(timeout=8.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError("stdin timeout did not fire")
    elapsed = time.time() - t0
    assert elapsed < 5.0
    assert rc == 0, "stdin timeout 経路は正常終了 (= 0)"
