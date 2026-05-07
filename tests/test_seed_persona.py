"""seed_persona の統合テスト — Ollama 落ちでも persona facts は seed される."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.seed_persona import seed


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "p.db"
    init_db(p)
    return p


def _seed_kwargs() -> dict:
    return dict(
        role="バックエンドエンジニアの相棒",
        name="テスト太郎",
        gender="男性",
        personality="冷静沈着",
        first_person="僕",
        speech_style="敬語 (丁寧)",
        address_user="あなた",
    )


def test_seed_inserts_all_persona_facts(db_path: Path):
    # Ollama 呼び出しを mock (空 vector を返す → embedding なしで保存)
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT key, value, importance FROM facts "
            "WHERE category='persona' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    keys = [r[0] for r in rows]
    # 7 つの基本 + 3 つのデフォルト行動指針 (response_brevity /
    # confirmation_before_acting / silent_memory) = 10
    assert len(rows) == 10
    assert "role" in keys
    assert "identity" in keys
    assert "personality" in keys
    assert "gender" in keys
    assert "first_person" in keys
    assert "speech_style" in keys
    assert "address_user" in keys
    assert "response_brevity" in keys
    assert "confirmation_before_acting" in keys
    assert "silent_memory" in keys


def test_seed_writes_embeddings_when_available(db_path: Path):
    fake_vec = [0.1] * 768
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = fake_vec
        seed(db_path, **_seed_kwargs())

    conn = connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM fact_embeddings").fetchone()[0]
    finally:
        conn.close()
    assert n == 10


def test_seed_idempotent(db_path: Path):
    """同じ key で 2 回 seed しても重複しない (active 1 件のみ)。"""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())
        seed(db_path, **{**_seed_kwargs(), "personality": "明るく前向き"})  # 値変更

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT value FROM facts WHERE category='persona' AND key='personality'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "明るく前向き"


def test_seed_persona_facts_are_boot_layer(db_path: Path):
    """seed されたものは on_session_start が boot 層として注入できる。"""
    from scripts.boot.inject import fetch_boot_facts

    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    conn = connect(db_path)
    try:
        facts = fetch_boot_facts(conn)
    finally:
        conn.close()

    # 全 10 件が persona category なので boot 層に出る
    assert len(facts) == 10
    assert all(f["category"] == "persona" for f in facts)
