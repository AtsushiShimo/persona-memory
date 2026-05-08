"""boot 層 default key が write LLM 経路から保護されているかのテスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.boot.defaults import DEFAULT_BOOT_FACTS, PROTECTED_KEYS
from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.write.extract import FactCandidate
from scripts.write.persist import apply_candidate, insert_new


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


def test_protected_keys_includes_all_defaults():
    """DEFAULT_BOOT_FACTS の全 (category, key) が PROTECTED_KEYS に入る."""
    expected = {(cat, key) for cat, key, _, _ in DEFAULT_BOOT_FACTS}
    assert PROTECTED_KEYS == expected


def test_apply_candidate_rejects_protected_natural_voice(db):
    """persona/natural_voice (default) を write LLM が上書きしようとしても reject."""
    # default を seed したフリで insert (実態としてあるとする)
    insert_new(db, FactCandidate("persona", "natural_voice", "原本", 9), [0.1] * 768)
    db.commit()

    # write LLM が無関係な内容で同 key を作ろうとする
    bad = FactCandidate("persona", "natural_voice", "renju の競合なし", 8)
    result = apply_candidate(db, bad, match=None, embedding=[0.5] * 768)
    assert result == "protected"

    # 原本が active のまま、上書きされていない
    rows = db.execute(
        "SELECT value, status FROM facts WHERE category='persona' AND key='natural_voice' ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "原本"
    assert rows[0][1] == "active"


def test_apply_candidate_rejects_protected_forbid_auto_memory(db):
    """rule/forbid_auto_memory も保護対象."""
    bad = FactCandidate("rule", "forbid_auto_memory", "別の値", 8)
    result = apply_candidate(db, bad, match=None, embedding=[0.5] * 768)
    assert result == "protected"


def test_apply_candidate_allows_user_persona_keys(db):
    """ユーザー固有の persona key (= default 外) は通常通り書ける."""
    # 'role' は default に存在 (init で seed されるが値はユーザー固有)
    # ただし PROTECTED_KEYS には role が含まれない (default リストにあるのは
    # response_brevity 等の default 行動指針のみ、role/identity 等の固有属性は
    # init/seed_persona.py 側で書かれる)
    user_fact = FactCandidate("persona", "tone_correction", "敬語をやめてほしい", 8)
    result = apply_candidate(db, user_fact, match=None, embedding=[0.1] * 768)
    assert result == "insert"


def test_apply_candidate_allows_other_categories(db):
    """preference/skill/profile/context/aversion は完全に保護対象外."""
    fact = FactCandidate("preference", "coffee", "深煎り", 6)
    result = apply_candidate(db, fact, match=None, embedding=[0.1] * 768)
    assert result == "insert"
