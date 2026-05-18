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


def spawn_cozo_topic_summary_backfill(db_path) -> None:
    """0.7.3: Cozo の topic_summary_emb が未生成な topic を遡及救済する detach 起動.

    対象は `topic.summary` null or `topic_summary_emb` 行無しの topic. 軽量
    light LLM で短い summary を生成 → embedding 化 → upsert. 1 session で
    全部処理する設計 (.cozo.db が大きい場合は数十秒〜数分かかる. detach なので
    SessionStart 体感は影響なし).

    PERSONA_TOPIC_DISABLE / PERSONA_TOPIC_SUMMARY_DISABLE で opt-out.
    マスターがコマンドを手動で叩く必要は無い経路.
    """
    if os.environ.get("PERSONA_TOPIC_DISABLE", "").strip() == "1":
        return
    if os.environ.get("PERSONA_TOPIC_SUMMARY_DISABLE", "").strip() == "1":
        return
    if db_path is None:
        return
    # Cozo DB 存在チェック (旧 SQLite のみのユーザーには起動しない)
    from pathlib import Path

    from scripts.db_cozo.wire import cozo_db_path_for
    cozo_db = cozo_db_path_for(Path(db_path))
    if not cozo_db.exists():
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "scripts.db_cozo.backfill_topic_summary",
             "--db", str(cozo_db)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass


def spawn_cozo_graph_backfill(db_path) -> None:
    """0.7.3: Cozo の discussion_node が未生成な episode を遡及救済する detach 起動.

    対象は episode_id に紐付く discussion_node が存在しない (= まだ
    backfill が当たっていない) episode. 重い処理 (1 episode 5-15s) なので
    完走に時間がかかるが、 detach + start_new_session で session を跨いでも
    継続実行される. 同じ DB に対して並列起動するのを避けるため、
    ロックファイル (cozo.backfill_graph.lock) で多重起動防止する.

    PERSONA_GRAPH_BACKFILL_DISABLE=1 で opt-out.
    マスターがコマンドを手動で叩く必要は無い経路.
    """
    if os.environ.get("PERSONA_GRAPH_BACKFILL_DISABLE", "").strip() == "1":
        return
    if db_path is None:
        return
    from pathlib import Path

    from scripts.db_cozo.wire import cozo_db_path_for
    cozo_db = cozo_db_path_for(Path(db_path))
    if not cozo_db.exists():
        return
    # 多重起動防止 lockfile (PID を書き込んで存在チェック).
    # 直接文字列連結 (with_suffix だと `.db` 部分が置換され `.cozo.backfill.lock` になる).
    lock = cozo_db.parent / f"{cozo_db.name}.backfill.lock"
    if lock.exists():
        try:
            pid_str = lock.read_text().strip()
            pid = int(pid_str) if pid_str else 0
        except Exception:
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, 0)  # 生存確認
                return  # 既に動いている → skip
            except OSError:
                pass  # PID 不在 → stale lock として上書き
        try:
            lock.unlink()
        except Exception:
            pass
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "scripts.db_cozo.backfill_graph",
             "--db", str(cozo_db), "--no-progress"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        try:
            lock.write_text(str(proc.pid))
        except Exception:
            pass
    except Exception:
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
