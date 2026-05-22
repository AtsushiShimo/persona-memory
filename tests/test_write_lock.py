"""write.run の多重起動防止 lock + pending 蓄積テスト (0.8.7).

問題: spawn_write が無条件で subprocess.Popen を呼ぶため、 ユーザのターン毎に
write.run プロセスが積み上がる. 1 プロセスが Ollama 待ちで数百秒占有することも
あり、 RAM 圧迫 / zombie 累積の温床になっていた.

対策: project 単位の `.persona-memory/write.run.lock` で多重起動を防ぎ、
skip 時は pending IDs を蓄積、 完走中の write.run が drain して拾い直す.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from scripts.shared.write_lock import (
    acquire_and_spawn,
    drain_pending,
    release,
)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Cozo DB のダミー path (lockfile は parent ディレクトリに置かれる)."""
    db = tmp_path / "ペルソナ.cozo.db"
    db.write_bytes(b"")
    return db


def _read_lock(db_path: Path) -> dict:
    lock = db_path.parent / "write.run.lock"
    return json.loads(lock.read_text(encoding="utf-8"))


class _RecordingSpawner:
    """subprocess.Popen の fake. 子プロセス PID を返すだけで実プロセスは起動しない."""

    def __init__(self, fake_pid: int = 99999):
        self.calls: list[list[int]] = []
        self.fake_pid = fake_pid

    def __call__(self, episode_ids: list[int]) -> int:
        self.calls.append(list(episode_ids))
        return self.fake_pid


def test_first_acquire_writes_pid_and_spawns(tmp_db: Path):
    """初回 acquire: lockfile が無ければ即 spawn し PID を書き込む."""
    sp = _RecordingSpawner(fake_pid=12345)
    acquired = acquire_and_spawn(tmp_db, [10, 11], spawn_fn=sp)
    assert acquired is True
    assert sp.calls == [[10, 11]]
    state = _read_lock(tmp_db)
    assert state["pid"] == 12345
    assert state["pending"] == []


def test_second_acquire_while_alive_appends_pending(tmp_db: Path):
    """既存 PID 生存中の 2 度目 acquire: pending に追記して skip."""
    my_pid = os.getpid()  # 必ず生存
    sp1 = _RecordingSpawner(fake_pid=my_pid)
    acquire_and_spawn(tmp_db, [10], spawn_fn=sp1)

    sp2 = _RecordingSpawner(fake_pid=88888)
    acquired = acquire_and_spawn(tmp_db, [20, 21], spawn_fn=sp2)
    assert acquired is False
    assert sp2.calls == [], "spawn してはいけない"
    state = _read_lock(tmp_db)
    assert state["pid"] == my_pid
    assert state["pending"] == [20, 21]


def test_third_acquire_appends_more(tmp_db: Path):
    """3 度目 acquire も pending に追記される (順序保持)."""
    my_pid = os.getpid()
    acquire_and_spawn(tmp_db, [10], spawn_fn=_RecordingSpawner(fake_pid=my_pid))
    acquire_and_spawn(tmp_db, [20], spawn_fn=_RecordingSpawner(fake_pid=88))
    acquire_and_spawn(tmp_db, [30, 31], spawn_fn=_RecordingSpawner(fake_pid=89))
    state = _read_lock(tmp_db)
    assert state["pending"] == [20, 30, 31]


def test_stale_pid_is_overtaken(tmp_db: Path):
    """死亡 PID が書かれた lockfile は stale 扱いで新 spawn が引き継ぐ."""
    lock = tmp_db.parent / "write.run.lock"
    # 存在しない PID (= 死亡) + pending 残ありの状態を仕込む
    lock.write_text(json.dumps({"pid": 1, "pending": [99]}), encoding="utf-8")

    sp = _RecordingSpawner(fake_pid=4242)
    acquired = acquire_and_spawn(tmp_db, [10], spawn_fn=sp)
    assert acquired is True
    # stale pending が新 spawn に引き継がれる
    assert sp.calls == [[99, 10]]
    state = _read_lock(tmp_db)
    assert state["pid"] == 4242
    assert state["pending"] == []


def test_pid_zero_is_stale(tmp_db: Path):
    """release が pending 残で PID=0 にした状態 → 次 spawn が引き継ぐ."""
    lock = tmp_db.parent / "write.run.lock"
    lock.write_text(json.dumps({"pid": 0, "pending": [77]}), encoding="utf-8")

    sp = _RecordingSpawner(fake_pid=5555)
    acquired = acquire_and_spawn(tmp_db, [88], spawn_fn=sp)
    assert acquired is True
    assert sp.calls == [[77, 88]]


def test_drain_pending_extracts_and_empties(tmp_db: Path):
    """drain_pending は pending を取り出して lockfile を空にする."""
    my_pid = os.getpid()
    acquire_and_spawn(tmp_db, [10], spawn_fn=_RecordingSpawner(fake_pid=my_pid))
    acquire_and_spawn(tmp_db, [20, 21], spawn_fn=_RecordingSpawner(fake_pid=99))
    acquire_and_spawn(tmp_db, [22], spawn_fn=_RecordingSpawner(fake_pid=99))

    pending = drain_pending(tmp_db)
    assert pending == [20, 21, 22]

    pending2 = drain_pending(tmp_db)
    assert pending2 == [], "drain 後は空"


def test_drain_pending_missing_lockfile_returns_empty(tmp_db: Path):
    """lockfile が無い状態の drain は空リスト."""
    assert drain_pending(tmp_db) == []


def test_release_when_pending_empty_unlinks(tmp_db: Path):
    """pending 空での release は lockfile を削除する."""
    my_pid = os.getpid()
    acquire_and_spawn(tmp_db, [10], spawn_fn=_RecordingSpawner(fake_pid=my_pid))
    drain_pending(tmp_db)  # 念のため空に
    release(tmp_db, my_pid)
    assert not (tmp_db.parent / "write.run.lock").exists()


def test_release_when_pending_nonempty_keeps_lockfile_with_pid_zero(tmp_db: Path):
    """pending 残ありで release: lockfile は残し、 PID=0 で次 spawn 引継ぎ可能にする."""
    my_pid = os.getpid()
    acquire_and_spawn(tmp_db, [10], spawn_fn=_RecordingSpawner(fake_pid=my_pid))
    acquire_and_spawn(tmp_db, [20], spawn_fn=_RecordingSpawner(fake_pid=99))
    # ここで drain せずに直接 release (= write.run 完走中に append された IDs)
    release(tmp_db, my_pid)
    lock = tmp_db.parent / "write.run.lock"
    assert lock.exists()
    state = json.loads(lock.read_text(encoding="utf-8"))
    assert state["pid"] == 0
    assert state["pending"] == [20]


def test_release_by_wrong_pid_is_noop(tmp_db: Path):
    """他人の PID で release を呼んでも lockfile は触らない (= 暴発防止)."""
    my_pid = os.getpid()
    acquire_and_spawn(tmp_db, [10], spawn_fn=_RecordingSpawner(fake_pid=my_pid))
    release(tmp_db, my_pid + 1)
    lock = tmp_db.parent / "write.run.lock"
    assert lock.exists()
    state = json.loads(lock.read_text(encoding="utf-8"))
    assert state["pid"] == my_pid


def test_empty_episode_ids_passed_to_acquire(tmp_db: Path):
    """空 episode_ids でも acquire は破綻しない."""
    sp = _RecordingSpawner(fake_pid=42)
    acquired = acquire_and_spawn(tmp_db, [], spawn_fn=sp)
    assert acquired is True
    assert sp.calls == [[]]
    state = _read_lock(tmp_db)
    assert state["pid"] == 42
    assert state["pending"] == []
