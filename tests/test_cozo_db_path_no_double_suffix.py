"""0.8.4 回帰テスト: `.cozo.db` 二重 suffix バグ防止.

背景: 0.8.3 で PERSONA_MEMORY_DB env テンプレを `<persona>.db` → `<persona>.cozo.db`
直接化に切り替えた際、 `Path(...).with_suffix(".cozo.db")` を素で呼ぶ callsite を
冪等版 `scripts.db_cozo.wire.cozo_db_path_for()` への置換し損ねた結果、
`Path("p.cozo.db").with_suffix(".cozo.db")` → `Path("p.cozo.cozo.db")` という
二重 suffix を生むバグが MCP server / health / hook spawn 経路で発症していた.

このテストは以下を保証する:
  1. 静的: 全 callsite が `cozo_db_path_for()` を経由 (素の with_suffix 禁止)
  2. server/db.py._cozo_path() が冪等
  3. scripts/health.py._check_db() が冪等
  4. scripts/shared/env.py.get_db_path() が冪等
  5. scripts/hooks/spawn.py が冪等な経路で cozo_db を解決
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── 1. 静的: 素の with_suffix(".cozo.db") 禁止 ─────────────────────────

def test_no_raw_with_suffix_cozo_db_outside_wire():
    """`with_suffix(".cozo.db")` を呼んでよいのは wire.py:cozo_db_path_for() のみ.

    他の callsite は冪等版 `cozo_db_path_for()` を経由しなければならない.
    そうしないと env が新形式 (`<persona>.cozo.db` 直接) の時に二重 suffix
    `.cozo.cozo.db` を生む.
    """
    pat = re.compile(r'\.with_suffix\(\s*["\']\.cozo\.db["\']\s*\)')
    allowed = {REPO_ROOT / "scripts" / "db_cozo" / "wire.py"}
    offenders: list[tuple[Path, int, str]] = []
    for root in ("scripts", "server"):
        for py in (REPO_ROOT / root).rglob("*.py"):
            if py in allowed:
                continue
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    offenders.append((py.relative_to(REPO_ROOT), i, line.strip()))
    assert not offenders, (
        "素の with_suffix(\".cozo.db\") が残存. cozo_db_path_for() を使うこと:\n"
        + "\n".join(f"  {p}:{i}  {s}" for p, i, s in offenders)
    )


# ── 2. server/db.py._cozo_path() が冪等 ────────────────────────────────

def test_server_db_cozo_path_idempotent_for_new_env_style(tmp_path, monkeypatch):
    """0.8.3 新形式: PERSONA_MEMORY_DB=<persona>.cozo.db 直接指定でも二重 suffix にならない."""
    pdir = tmp_path / ".persona-memory"
    pdir.mkdir()
    cozo_path = pdir / "t.cozo.db"
    cozo_path.touch()  # db_path() の exists() チェック通過用
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(cozo_path))

    from server import db
    resolved = db._cozo_path()
    assert resolved == cozo_path, (
        f"二重 suffix 発生: expected {cozo_path}, got {resolved}"
    )
    assert not str(resolved).endswith(".cozo.cozo.db")


def test_server_db_cozo_path_legacy_sqlite_style(tmp_path, monkeypatch):
    """旧形式: PERSONA_MEMORY_DB=<persona>.db でも `.cozo.db` に解決される (後方互換)."""
    pdir = tmp_path / ".persona-memory"
    pdir.mkdir()
    db_path = pdir / "t.db"
    db_path.touch()
    cozo_path = pdir / "t.cozo.db"
    cozo_path.touch()
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db_path))

    from server import db
    resolved = db._cozo_path()
    assert resolved == cozo_path


# ── 3. scripts/health.py._check_db() が冪等 ────────────────────────────

def test_health_check_db_idempotent_for_new_env_style(tmp_path):
    """0.8.3 新形式: `.cozo.db` 直接を渡しても DB を発見できる."""
    from scripts.db_cozo.connection import init_db
    from scripts.health import _check_db

    cozo_path = tmp_path / "t.cozo.db"
    init_db(cozo_path)  # 実 DB を作成

    result = _check_db(cozo_path)
    assert result["ok"] is True, f"health が DB を発見できず: {result}"
    assert result["path"].endswith("t.cozo.db")
    assert ".cozo.cozo.db" not in result["path"]


def test_health_check_db_legacy_sqlite_style(tmp_path):
    """旧形式: `.db` を渡しても `.cozo.db` を見に行ける."""
    from scripts.db_cozo.connection import init_db
    from scripts.health import _check_db

    db_path = tmp_path / "t.db"
    db_path.touch()
    cozo_path = tmp_path / "t.cozo.db"
    init_db(cozo_path)

    result = _check_db(db_path)
    assert result["ok"] is True, f"health が後方互換解決できず: {result}"


# ── 4. scripts/shared/env.py.get_db_path() が冪等 ──────────────────────

def test_shared_env_get_db_path_idempotent_for_new_env_style(tmp_path, monkeypatch):
    """0.8.3 新形式 env でも path を正しく返す."""
    cozo_path = tmp_path / "t.cozo.db"
    cozo_path.touch()
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(cozo_path))

    from scripts.shared.env import get_db_path
    result = get_db_path()
    assert result == cozo_path
    assert result is not None and not str(result).endswith(".cozo.cozo.db")


def test_shared_env_get_db_path_legacy_sqlite_style(tmp_path, monkeypatch):
    """旧形式 env (`<persona>.db`) でも `.cozo.db` に振り直して解決できる."""
    db_path = tmp_path / "t.db"
    db_path.touch()
    cozo_path = tmp_path / "t.cozo.db"
    cozo_path.touch()
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db_path))

    from scripts.shared.env import get_db_path
    result = get_db_path()
    assert result == cozo_path


# ── 5. hooks/spawn.py の cozo_db 解決経路が冪等 ────────────────────────

def test_hooks_spawn_topic_summary_resolves_cozo_db_idempotently(tmp_path, monkeypatch):
    """spawn_cozo_topic_summary が新形式 `.cozo.db` env で no-op せず subprocess を呼ぶ.

    内部実装で `.cozo.cozo.db` を見に行くと exists() が False になり no-op する.
    fixture で `.cozo.db` を実在させた上で subprocess.Popen 呼出有無を観測する.
    """
    cozo_path = tmp_path / "t.cozo.db"
    cozo_path.touch()

    called: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        called.append(cmd)
        class _P:
            pid = 1
        return _P()

    monkeypatch.setattr("scripts.hooks.spawn.subprocess.Popen", fake_popen)
    from scripts.hooks.spawn import spawn_cozo_topic_summary_backfill
    spawn_cozo_topic_summary_backfill(cozo_path)

    assert called, (
        "spawn_cozo_topic_summary_backfill が新形式 .cozo.db env で no-op した "
        "(= 二重 suffix で exists() に外れている)"
    )
    # 呼ばれた subprocess に渡された --db が二重 suffix でない
    assert all(".cozo.cozo.db" not in str(arg) for cmd in called for arg in cmd)


def test_hooks_spawn_graph_backfill_resolves_cozo_db_idempotently(tmp_path, monkeypatch):
    """spawn_cozo_graph_backfill も同様."""
    cozo_path = tmp_path / "t.cozo.db"
    cozo_path.touch()

    called: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        called.append(cmd)
        class _P:
            pid = 1
        return _P()

    monkeypatch.setattr("scripts.hooks.spawn.subprocess.Popen", fake_popen)
    from scripts.hooks.spawn import spawn_cozo_graph_backfill
    spawn_cozo_graph_backfill(cozo_path)

    assert called, (
        "spawn_cozo_graph_backfill が新形式 .cozo.db env で no-op した"
    )
    assert all(".cozo.cozo.db" not in str(arg) for cmd in called for arg in cmd)
    # backfill lock ファイルも二重 suffix でないこと (`.cozo.cozo.db.backfill.lock` 防止)
    locks = list(tmp_path.glob("*.backfill.lock"))
    assert all(".cozo.cozo.db" not in lk.name for lk in locks), (
        f"lock ファイル名に二重 suffix: {[str(l) for l in locks]}"
    )
