"""Ollama システムワイドセマフォテスト (0.8.7).

複数プロジェクトが同時起動した時に Ollama サーバへの並列リクエストを
N 個に制限する. write.run の多重起動 lock (project 単位) と組み合わせて、
RAM 圧迫 / Ollama queue 詰まりを抑える.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from scripts.shared.ollama_semaphore import ollama_slot


@pytest.fixture
def sema_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sema"
    d.mkdir()
    return d


def test_single_acquire_immediate(sema_dir: Path):
    """1 個取得は即時."""
    t0 = time.time()
    with ollama_slot(n=2, dir_path=sema_dir):
        pass
    assert time.time() - t0 < 0.5


def test_two_parallel_acquire_immediate(sema_dir: Path):
    """N=2 で 2 並列は両方とも即時取得できる."""
    started = [threading.Event(), threading.Event()]
    release = threading.Event()
    elapsed: list[float] = [-1.0, -1.0]

    def worker(idx: int):
        t0 = time.time()
        with ollama_slot(n=2, dir_path=sema_dir):
            elapsed[idx] = time.time() - t0
            started[idx].set()
            release.wait(timeout=5.0)

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    assert started[0].wait(timeout=2.0), "thread 0 が取得できない"
    assert started[1].wait(timeout=2.0), "thread 1 が取得できない (= 2 並列でブロックしてはいけない)"
    release.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert elapsed[0] < 1.0
    assert elapsed[1] < 1.0


def test_third_blocks_until_release(sema_dir: Path):
    """N=2 の 3 並列目は前 2 つのどちらかが release されるまでブロック."""
    started = [threading.Event(), threading.Event(), threading.Event()]
    release_first_two = threading.Event()
    release_third = threading.Event()

    def occupier(idx: int):
        with ollama_slot(n=2, dir_path=sema_dir):
            started[idx].set()
            release_first_two.wait(timeout=10.0)

    def third_worker():
        with ollama_slot(n=2, dir_path=sema_dir):
            started[2].set()
            release_third.wait(timeout=10.0)

    t1 = threading.Thread(target=occupier, args=(0,))
    t2 = threading.Thread(target=occupier, args=(1,))
    t1.start()
    t2.start()
    assert started[0].wait(timeout=2.0)
    assert started[1].wait(timeout=2.0)

    t3 = threading.Thread(target=third_worker)
    t3.start()
    # 3 つ目は 1 秒以内には入れない (= ブロックしている)
    assert not started[2].wait(timeout=1.0), \
        "N=2 のはずなのに 3 並列目がブロックされていない"

    # 1 つ release すれば 3 つ目が入る
    release_first_two.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert started[2].wait(timeout=3.0), "release 後 3 つ目が取得できない"

    release_third.set()
    t3.join(timeout=5.0)


def test_zero_disables(sema_dir: Path):
    """n=0 / 負数で制御を素通し (= 何個並列でも通る)."""
    started = [threading.Event() for _ in range(5)]
    release = threading.Event()

    def worker(idx: int):
        with ollama_slot(n=0, dir_path=sema_dir):
            started[idx].set()
            release.wait(timeout=5.0)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for i in range(5):
        assert started[i].wait(timeout=2.0), f"n=0 で thread {i} がブロックされた"
    release.set()
    for t in threads:
        t.join(timeout=5.0)


def test_exception_in_block_releases_slot(sema_dir: Path):
    """yield 内で例外が起きてもスロットは確実に release される."""
    with pytest.raises(RuntimeError):
        with ollama_slot(n=1, dir_path=sema_dir):
            raise RuntimeError("test")
    # 次の取得が即時できる (前の release が走っている証拠)
    t0 = time.time()
    with ollama_slot(n=1, dir_path=sema_dir):
        pass
    assert time.time() - t0 < 0.5
