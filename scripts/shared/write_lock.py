"""write.run の多重起動防止 + pending IDs 蓄積 lock (0.8.7).

設計:
- lockfile: `<persona-memory dir>/write.run.lock`
- 内容 (JSON): {"pid": int, "pending": [int, ...]}
- `fcntl.flock(LOCK_EX)` で read-modify-write を排他化.

経路:
- spawn 側 (`acquire_and_spawn`): flock 内で「PID 生存判定 → 起動権 or pending 追記」
  を atomic に行い、 spawn まで flock 保持. PID=0 の中間状態を外部に見せない.
- write.run 側 (`drain_pending` / `release`): flock 内で pending を吸い込み, 完走時に
  pending が残っていれば PID=0 で lockfile を残す (= 次 spawn が引き継ぐ stale 扱い).

理由: 過去, spawn_write が無条件で subprocess.Popen を呼んでいたため,
ユーザのターン毎に write.run が積み上がり, 1 プロセスが Ollama 待ちで数百秒
占有することもあり RAM 圧迫 / zombie 累積の温床になっていた.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Callable


def _lock_path_for(db_path: Path) -> Path:
    return db_path.parent / "write.run.lock"


def _is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_state(fd: int) -> dict:
    os.lseek(fd, 0, 0)
    raw = os.read(fd, 1 << 20).decode("utf-8").strip()
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _write_state(fd: int, state: dict) -> None:
    blob = json.dumps(state, ensure_ascii=False).encode("utf-8")
    os.lseek(fd, 0, 0)
    os.ftruncate(fd, 0)
    os.write(fd, blob)


def acquire_and_spawn(
    db_path: Path,
    episode_ids: list[int],
    spawn_fn: Callable[[list[int]], int],
) -> bool:
    """flock 内で起動権を判定し、 取得時は `spawn_fn` を呼んで PID を記録する.

    Args:
        db_path: Cozo DB の path. 親ディレクトリに lockfile を置く.
        episode_ids: 渡したい episode id 群. 起動権獲得時は前任の pending と
            合わせて spawn_fn に渡す. skip 時は pending に追記される.
        spawn_fn: 子プロセスを起動する関数. 引数 = stdin に流す episode_ids,
            戻り値 = 子プロセス PID. flock 保持中に呼ばれるため、 速やかに
            return すること (= Popen まで).

    Returns:
        True = 起動権を獲得し spawn_fn を呼んだ.
        False = 既存 write.run 生存中. pending に追記し spawn skip.
    """
    lock_path = _lock_path_for(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = _read_state(fd)
        pid = int(state.get("pid", 0) or 0)
        pending: list[int] = [int(x) for x in (state.get("pending") or [])]
        new_ids = [int(x) for x in episode_ids]

        if pid > 0 and _is_alive(pid):
            # 既存 write.run 生存中 → pending に追記して skip
            pending.extend(new_ids)
            _write_state(fd, {"pid": pid, "pending": pending})
            return False

        # PID 死亡 (= stale) or PID=0 (= 前任 release 残置) or 新規 → 起動権獲得
        # 前任の pending と合わせて引き継ぐ
        combined = pending + new_ids
        child_pid = int(spawn_fn(combined))
        _write_state(fd, {"pid": child_pid, "pending": []})
        return True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def drain_pending(db_path: Path) -> list[int]:
    """write.run 側で呼ぶ. 現在の pending を取り出して空にして返す.

    lockfile が無い場合は空リスト. 別 spawn が pending を追加する race は
    flock で直列化される.
    """
    lock_path = _lock_path_for(db_path)
    if not lock_path.exists():
        return []
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
    except FileNotFoundError:
        return []
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = _read_state(fd)
        pending = [int(x) for x in (state.get("pending") or [])]
        state["pending"] = []
        _write_state(fd, state)
        return pending
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def release(db_path: Path, my_pid: int) -> None:
    """write.run 完走時の lockfile 片付け.

    - pending 空 → unlink (= クリーン release).
    - pending 残あり → PID を 0 にして lockfile を残す. 次 spawn が stale と判定し
      pending を引き継いで起動する (= 取りこぼし無し).
    - 他人の PID が書かれていれば触らない (= 暴発防止).
    """
    lock_path = _lock_path_for(db_path)
    if not lock_path.exists():
        return
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
    except FileNotFoundError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = _read_state(fd)
        cur_pid = int(state.get("pid", 0) or 0)
        if cur_pid != int(my_pid):
            return
        pending = [int(x) for x in (state.get("pending") or [])]
        if pending:
            state["pid"] = 0
            _write_state(fd, state)
        else:
            try:
                os.unlink(str(lock_path))
            except OSError:
                pass
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
