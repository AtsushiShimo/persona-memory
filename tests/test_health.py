"""scripts.health の包括的 health check テスト."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode, set_meta
from scripts.health import _check_db, _check_recent_ingest, collect
from scripts.shared.embedding import pack
from scripts.write.extract import FactCandidate
from scripts.write.persist import insert_new


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "p.db"
    init_db(p)
    return p


def test_check_db_reports_ok_for_valid_db(db_path):
    out = _check_db(db_path)
    assert out["ok"] is True
    assert out["schema_version"] == out["schema_version_expected"]
    assert out["facts_active"] == 0
    assert out["episodes"] == 0
    assert out["fact_embeddings"] == 0
    assert out["orphan_active_facts"] == 0
    assert out["stale_embeddings"] == 0


def test_check_db_reports_error_for_missing_db(tmp_path):
    out = _check_db(tmp_path / "missing.db")
    assert out["ok"] is False
    assert "存在しない" in out["error"]


def test_check_db_detects_orphan_active_fact(db_path):
    """active fact だけ作って embedding を渡さない → orphan としてカウント."""
    conn = connect(db_path)
    try:
        insert_new(conn, FactCandidate("preference", "x", "v", 5), embedding=None)
        conn.commit()
    finally:
        conn.close()
    out = _check_db(db_path)
    assert out["orphan_active_facts"] == 1


def test_check_db_detects_stale_embedding(db_path):
    """active fact が無い fact_id に embedding だけ残す → stale としてカウント."""
    conn = connect(db_path)
    try:
        # active fact を作って supersede 状態にする (embedding を残したまま)
        fid = insert_new(
            conn, FactCandidate("preference", "x", "v", 5),
            embedding=[1.0] + [0.0] * 767,
        )
        conn.execute("UPDATE facts SET status='superseded' WHERE id=?", (fid,))
        conn.commit()
    finally:
        conn.close()
    out = _check_db(db_path)
    assert out["stale_embeddings"] == 1


def test_check_recent_ingest_pending_episodes(db_path):
    """write_processed_max_id < latest episode id → pending としてカウント."""
    conn = connect(db_path)
    try:
        for i in range(3):
            save_episode(conn, role="user", content=f"hello {i}", session_id="s")
        set_meta(conn, "write_processed_max_id", "1")
    finally:
        conn.close()
    out = _check_recent_ingest(db_path)
    assert out["last_episode_id"] == 3
    assert out["write_processed_max_id"] == 1
    assert out["episodes_pending_write"] == 2


def test_check_recent_ingest_short_value_facts(db_path):
    """3 文字以下 value の fact をカウントする (write LLM 品質 sanity)."""
    conn = connect(db_path)
    try:
        for k, v in [("a", "短い"), ("b", "もっと長い値"), ("c", "x")]:
            insert_new(
                conn, FactCandidate("preference", k, v, 5),
                embedding=[1.0] + [0.0] * 767,
            )
        conn.commit()
    finally:
        conn.close()
    out = _check_recent_ingest(db_path)
    assert out["recent_facts_with_short_value"] == 2  # "短い" と "x"


def test_collect_top_level_aggregation(db_path, monkeypatch):
    """collect() の集計が ok / warnings / errors に正しく反映される."""
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db_path))
    # Ollama 部分は mock で reachable=True にする
    fake_ollama = {
        "host": "http://localhost:11434",
        "reachable": True,
        "models_pulled": {"embed": True, "light": True, "heavy": True},
        "models_required": {"embed": "x", "light": "y", "heavy": "z"},
        "num_parallel_env": "(unset)",
    }
    with patch("scripts.health._check_ollama", return_value=fake_ollama):
        out = collect(db_path)
    assert out["ok"] is True
    assert out["errors"] == []
    assert "db" in out
    assert "config" in out
    assert "ollama" in out
    assert "recent_ingest" in out


def test_collect_errors_when_ollama_unreachable(db_path, monkeypatch):
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db_path))
    fake_ollama = {
        "host": "http://localhost:11434",
        "reachable": False,
        "error": "Connection refused",
        "num_parallel_env": "(unset)",
    }
    with patch("scripts.health._check_ollama", return_value=fake_ollama):
        out = collect(db_path)
    assert out["ok"] is False
    assert any("Ollama 到達不可" in e for e in out["errors"])


def test_collect_warns_on_stale_embeddings(db_path, monkeypatch):
    """stale embedding があると warnings に出る."""
    conn = connect(db_path)
    try:
        fid = insert_new(
            conn, FactCandidate("preference", "x", "v", 5),
            embedding=[1.0] + [0.0] * 767,
        )
        conn.execute("UPDATE facts SET status='superseded' WHERE id=?", (fid,))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("PERSONA_MEMORY_DB", str(db_path))
    fake_ollama = {
        "host": "x", "reachable": True,
        "models_pulled": {"embed": True, "light": True, "heavy": True},
        "models_required": {"embed": "x", "light": "y", "heavy": "z"},
        "num_parallel_env": "(unset)",
    }
    with patch("scripts.health._check_ollama", return_value=fake_ollama):
        out = collect(db_path)
    assert any("stale embedding" in w for w in out["warnings"])


def test_collect_no_db_path():
    """PERSONA_MEMORY_DB env も引数も無い → DB エラー."""
    with patch.dict("os.environ", {"PERSONA_MEMORY_DB": ""}, clear=False):
        out = collect(None)
    assert out["ok"] is False
    assert any("PERSONA_MEMORY_DB" in e for e in out["errors"])
