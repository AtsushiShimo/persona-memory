"""detach 子プロセスとして write LLM を起動するヘルパ。

PERSONA_WRITE_DISABLE=1 でテスト時に無効化できる (= 実 LLM を呼ばない)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def spawn_write(episode_ids: list[int]) -> None:
    """write.run を多重起動防止 lock 経由で detach 起動.

    既存 write.run 生存中は spawn せず、 episode_ids を lockfile の pending に
    追記する. 完走中の write.run が drain して拾い直す (= 取りこぼし無し).
    詳細: scripts/shared/write_lock.py.
    """
    if os.environ.get("PERSONA_WRITE_DISABLE") == "1":
        return
    if not episode_ids:
        return

    try:
        from scripts.shared.env import get_db_path
        from scripts.shared.write_lock import acquire_and_spawn
        db_path = get_db_path()
    except Exception:
        db_path = None

    if db_path is None:
        _spawn_write_unlocked(episode_ids)
        return

    def _do_spawn(ids: list[int]) -> int:
        payload = json.dumps({"episode_ids": ids}).encode()
        p = subprocess.Popen(
            [sys.executable, "-m", "scripts.write.run"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        if p.stdin:
            p.stdin.write(payload)
            p.stdin.close()
        return p.pid

    try:
        acquire_and_spawn(db_path, list(episode_ids), spawn_fn=_do_spawn)
    except Exception:
        # fail-open: lock 取得失敗でも raw 保存は完了済み
        pass


def _spawn_write_unlocked(episode_ids: list[int]) -> None:
    """lock 経路が使えない時の fallback (旧挙動と同等)."""
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
        if p.stdin:
            p.stdin.write(payload)
            p.stdin.close()
    except Exception:
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


