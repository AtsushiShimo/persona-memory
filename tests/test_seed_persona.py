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
    """boot 層 = persona 13 + rule 2 = 15 件 seed される。"""
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

    # persona: 7 基本 + 6 default 行動指針 = 13
    assert len(persona_rows) == 13
    for k in (
        "role", "identity", "personality", "gender", "first_person",
        "speech_style", "address_user",
        "response_brevity", "confirmation_before_acting", "silent_memory",
        "natural_voice", "health_check_trigger", "explicit_recall_via_mcp",
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
    assert n == 15  # persona 13 + rule 2


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

    # boot 層 = persona 13 + rule 2 = 15 件
    assert len(facts) == 15
    cats = {f["category"] for f in facts}
    assert cats == {"persona", "rule"}


def test_seed_without_stance_does_not_create_stance_fact(db_path: Path):
    """stance 省略時は persona/stance fact が作られない (後方互換)."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())  # stance 引数なし

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM facts WHERE category='persona' AND key='stance'"
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_seed_with_stance_creates_natural_language_fact(db_path: Path):
    """stance 5 値で seed → persona/stance fact が自然語 value で作られる."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        # 革新 +1 / 悲観 0 / 分析 -1 / 大胆 +2 / 共感 -1
        seed(db_path, **{**_seed_kwargs(), "stance": [1, 0, -1, 2, -1]})

    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT value, importance FROM facts "
            "WHERE category='persona' AND key='stance' AND status='active'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    value, importance = row[0], row[1]
    assert importance == 9
    # 5 軸全てが value に含まれている
    assert "保守-革新軸" in value
    assert "楽観-悲観軸" in value
    assert "直感-分析軸" in value
    assert "慎重-大胆軸" in value
    assert "共感-論理軸" in value
    # 各タグの方向が値どおりに記述されている
    assert "やや革新寄り" in value     # +1
    assert "バランス" in value          # 0
    assert "やや直感寄り" in value     # -1 (左 = 直感)
    assert "強く大胆寄り" in value     # +2
    assert "やや共感寄り" in value     # -1


def test_parse_stance_csv_validates_format():
    """--stance CSV のパーサが要素数 / 範囲 / 整数を検証."""
    from scripts.seed_persona import _parse_stance_csv

    # 正常
    assert _parse_stance_csv("0,1,-1,2,-2") == [0, 1, -1, 2, -2]
    assert _parse_stance_csv(" 0 , 1 , -1 , 2 , -2 ") == [0, 1, -1, 2, -2]

    # 異常
    with pytest.raises(ValueError, match="5 要素必要"):
        _parse_stance_csv("0,1,-1")
    with pytest.raises(ValueError, match="整数でない"):
        _parse_stance_csv("0,1,abc,2,-1")
    with pytest.raises(ValueError, match=r"-2\.\.\+2"):
        _parse_stance_csv("0,1,-3,2,-1")
    with pytest.raises(ValueError, match=r"-2\.\.\+2"):
        _parse_stance_csv("0,1,1,2,3")
