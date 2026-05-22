"""Ollama サーバへの同時リクエスト数を全プロジェクト跨ぎで N に制限する semaphore (0.8.7).

複数 persona プロジェクトが並列起動すると、 各々の write.run lock は独立なので、
結局 1 つの Ollama サーバに同時 N 個以上のリクエストが投げられて queue 詰まり /
RAM 圧迫を引き起こす. 本モジュールは `/tmp/persona-memory-ollama/slot-{0..N-1}.lock`
で flock セマフォを実装し、 N=2 (既定) を超えた並列を blocking 待機させる.

無効化: `PERSONA_OLLAMA_CONCURRENCY=0` (環境変数) で素通し.
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DEFAULT_N = int(os.environ.get("PERSONA_OLLAMA_CONCURRENCY", "2"))
DEFAULT_DIR = Path(os.environ.get(
    "PERSONA_OLLAMA_SEMA_DIR", "/tmp/persona-memory-ollama",
))


def _ensure_dir(d: Path) -> None:
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


@contextmanager
def ollama_slot(
    n: int = DEFAULT_N, dir_path: Path = DEFAULT_DIR,
) -> Iterator[None]:
    """Ollama 呼び出しを覆う context manager.

    挙動:
    - n <= 0: 素通し (= 制御無効).
    - 1..n のスロットを非ブロッキングで順に try, 取れた fd を保持して yield.
    - 全スロット埋まり: slot-0 を blocking で待機 (= 空いた瞬間に進む).
    - 例外発生時も yield 後の release は必ず実行される.
    """
    if n <= 0:
        yield
        return
    _ensure_dir(dir_path)
    held_fd: int | None = None
    try:
        for i in range(n):
            slot = dir_path / f"slot-{i}.lock"
            fd = os.open(str(slot), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held_fd = fd
                break
            except BlockingIOError:
                os.close(fd)
        if held_fd is None:
            # 全スロット埋まり → slot-0 を blocking で待つ
            slot = dir_path / "slot-0.lock"
            held_fd = os.open(str(slot), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(held_fd, fcntl.LOCK_EX)
        yield
    finally:
        if held_fd is not None:
            try:
                fcntl.flock(held_fd, fcntl.LOCK_UN)
            finally:
                os.close(held_fd)
