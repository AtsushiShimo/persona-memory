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


def test_seed_inserts_all_boot_facts(db_path: Path):
    """boot 層 = persona 11 + rule 2 = 13 件 seed される。"""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    conn = connect(db_path)
    try:
        persona_rows = conn.execute(
            "SELECT key FROM facts WHERE category='persona' ORDER BY id"
        ).fetchall()
        rule_rows = conn.execute(
            "SELECT key FROM facts WHERE category='rule' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    persona_keys = [r[0] for r in persona_rows]
    rule_keys = [r[0] for r in rule_rows]

    # persona: 7 基本 + 4 default 行動指針 = 11
    assert len(persona_rows) == 11
    for k in (
        "role", "identity", "personality", "gender", "first_person",
        "speech_style", "address_user",
        "response_brevity", "confirmation_before_acting", "silent_memory",
        "natural_voice",
    ):
        assert k in persona_keys, f"missing persona/{k}"

    # rule: no_direct_memory_lookup + forbid_auto_memory
    assert len(rule_rows) == 2
    assert "no_direct_memory_lookup" in rule_keys
    assert "forbid_auto_memory" in rule_keys


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
    assert n == 13  # persona 11 + rule 2


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

    # boot 層 = persona 11 + rule 2 = 13 件
    assert len(facts) == 13
    cats = {f["category"] for f in facts}
    assert cats == {"persona", "rule"}
