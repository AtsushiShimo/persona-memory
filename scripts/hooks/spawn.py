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


def spawn_lint(fact_ids: list[int], trigger: str = "write_tail") -> None:
    """write LLM 完了後の tail として lint を detach 起動.

    fact_ids: 直前 write で active 化された / 触られた fact の id 群.
    その近傍に対して judge_conflict を走らせ, auto_resolve / flag する.
    PERSONA_LINT_DISABLE=1 で無効化 (テスト時用).
    """
    if os.environ.get("PERSONA_LINT_DISABLE") == "1":
        return
    if not fact_ids:
        return

    payload = json.dumps({"fact_ids": fact_ids, "trigger": trigger}).encode()
    try:
        p = subprocess.Popen(
            [sys.executable, "-m", "scripts.lint.run"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        if p.stdin:
            p.stdin.write(payload)
            p.stdin.close()
    except Exception:
        # fail-open: lint 失敗でも write 結果は確定済み
        pass


def spawn_prewarm() -> None:
    """SessionStart で Ollama heavy + embed モデルを background で warm-up.

    PERSONA_PREWARM_DISABLE=1 でテスト / opt-out.
    fail-open: ollama 未起動などは静かに諦める (子プロセス側で吸収).
    """
    if os.environ.get("PERSONA_PREWARM_DISABLE") == "1":
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "scripts.shared.prewarm"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass
