"""upgrade (non-destructive default refresh) のテスト."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.boot.defaults import DEFAULT_BOOT_FACTS
from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.seed_persona import seed
from scripts.upgrade import upgrade


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


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "p.db"
    init_db(p)
    return p


def test_upgrade_unchanged_after_fresh_seed(db_path: Path):
    """seed 直後に upgrade を走らせると、何も変わらない (idempotent)."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)

    assert counts["inserted"] == 0
    assert counts["updated"] == 0
    assert counts["unchanged"] == len(DEFAULT_BOOT_FACTS)


def test_upgrade_inserts_missing_defaults(db_path: Path):
    """default が一つも無い DB に upgrade を走らせると全部 insert."""
    # seed_persona は呼ばない = ペルソナ属性も default も無い空の DB
    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)

    assert counts["inserted"] == len(DEFAULT_BOOT_FACTS)
    assert counts["updated"] == 0
    assert counts["unchanged"] == 0


def test_upgrade_updates_changed_default(db_path: Path):
    """既存 default の value が古いと、supersede + 新規 insert."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    # response_brevity の値を古いダミーに置き換える (= プラグイン旧版が
    # 入れていた古い文言という想定)
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE facts SET value='OLD_VERSION' "
            "WHERE category='persona' AND key='response_brevity' AND status='active'"
        )
        conn.commit()
    finally:
        conn.close()

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        counts = upgrade(db_path)

    assert counts["updated"] == 1
    assert counts["inserted"] == 0
    assert counts["unchanged"] == len(DEFAULT_BOOT_FACTS) - 1

    # 新版が active、旧版は superseded、リンクが張られている
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, value, status, supersedes, superseded_by FROM facts "
            "WHERE category='persona' AND key='response_brevity' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    old, new = rows
    assert old[1] == "OLD_VERSION"
    assert old[2] == "superseded"
    assert old[4] == new[0]  # superseded_by points to new
    assert new[2] == "active"
    assert new[3] == old[0]  # supersedes points to old


def test_upgrade_does_not_touch_persona_specific_facts(db_path: Path):
    """ペルソナ固有属性 (role/identity/...) は upgrade で触らない."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    conn = connect(db_path)
    try:
        before = conn.execute(
            "SELECT category, key, value FROM facts "
            "WHERE category='persona' AND key IN "
            "  ('role','identity','personality','gender','first_person',"
            "   'speech_style','address_user') "
            "ORDER BY key"
        ).fetchall()
    finally:
        conn.close()

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        upgrade(db_path)

    conn = connect(db_path)
    try:
        after = conn.execute(
            "SELECT category, key, value FROM facts "
            "WHERE category='persona' AND key IN "
            "  ('role','identity','personality','gender','first_person',"
            "   'speech_style','address_user') "
            "ORDER BY key"
        ).fetchall()
    finally:
        conn.close()

    assert before == after  # 完全一致 = 触られていない


def test_upgrade_does_not_touch_episodes_and_other_categories(db_path: Path):
    """episodes と preference / aversion / profile / skill / context も無傷."""
    with patch("scripts.seed_persona.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        seed(db_path, **_seed_kwargs())

    # ダミーの user データを足す
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO facts(category, key, value, importance) "
            "VALUES ('preference', 'coffee', '深煎り', 6)"
        )
        conn.execute(
            "INSERT INTO facts(category, key, value, importance) "
            "VALUES ('aversion', 'sweets', '糖尿病で控えてる', 7)"
        )
        conn.execute(
            "INSERT INTO episodes(role, content, session_id) "
            "VALUES ('user', '昨日の話', 's1')"
        )
        conn.commit()
    finally:
        conn.close()

    with patch("scripts.upgrade.OllamaClient") as Mock:
        Mock.return_value.embed.return_value = []
        upgrade(db_path)

    conn = connect(db_path)
    try:
        pref = conn.execute(
            "SELECT value FROM facts WHERE category='preference' AND key='coffee'"
        ).fetchone()
        aver = conn.execute(
            "SELECT value FROM facts WHERE category='aversion' AND key='sweets'"
        ).fetchone()
        ep = conn.execute(
            "SELECT content FROM episodes WHERE session_id='s1'"
        ).fetchone()
    finally:
        conn.close()

    assert pref[0] == "深煎り"
    assert aver[0] == "糖尿病で控えてる"
    assert ep[0] == "昨日の話"
