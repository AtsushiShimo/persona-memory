"""write.run の stdin hang 対策テスト (0.8.6).

問題: spawn 側が payload を stdin に流す前にハーネスごと死ぬと、 子 write.run は
EOF が来ない stdin を永遠に待つ. start_new_session=True で切り離されているので
親死亡では巻き込まれない. 過去 11 個の zombie 累積の正体.

修正: main() で stdin に reasonable timeout (~30s) を入れ、 payload 不着なら exit.
"""
from __future__ import annotations

import subprocess
import sys
import time


def test_write_run_exits_when_stdin_idle():
    """親が stdin を閉じない / 書き込まないままなら、 write.run は 35 秒以内に exit する.

    現状: json.load(sys.stdin) で永遠に待つ → このテストは 35 秒で timeout して失敗.
    修正後: 30 秒の select timeout で抜けて exit 0.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.write.run"],
        stdin=subprocess.PIPE,  # 開くが書かない / 閉じない
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    t0 = time.time()
    try:
        rc = proc.wait(timeout=35.0)
        elapsed = time.time() - t0
        assert rc == 0, f"unexpected exit code {rc}"
        assert elapsed < 35.0, f"took too long: {elapsed:.1f}s"
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError(
            "write.run hung waiting for stdin (= 19 時間 zombie の原因). "
            "main() に stdin timeout を入れる必要がある."
        )


def test_write_run_processes_payload_when_stdin_closed_normally():
    """payload を書いて stdin を閉じた通常経路は壊れない (回帰防止)."""
    payload = '{"episode_ids": []}'  # 空でも main() は完走するはず
    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.write.run"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.stdin.write(payload.encode())
    proc.stdin.close()
    rc = proc.wait(timeout=15.0)
    assert rc == 0
