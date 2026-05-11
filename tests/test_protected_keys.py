"""boot 層 default key が write LLM 経路から保護されているかのテスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.boot.defaults import (
    DEFAULT_BOOT_FACTS,
    PERSONA_CORE_KEYS,
    PROTECTED_KEYS,
)
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
    defaults = {(cat, key) for cat, key, _, _ in DEFAULT_BOOT_FACTS}
    assert defaults <= PROTECTED_KEYS  # defaults は PROTECTED_KEYS の subset


def test_protected_keys_includes_persona_core():
    """0.5.26: ペルソナ固有 9 属性も PROTECTED_KEYS に含まれる."""
    assert PERSONA_CORE_KEYS <= PROTECTED_KEYS
    # 具体的に: identity / role / address_user 等は確実に保護される
    assert ("persona", "identity") in PROTECTED_KEYS
    assert ("persona", "role") in PROTECTED_KEYS
    assert ("persona", "address_user") in PROTECTED_KEYS


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
    """default にも PERSONA_CORE_KEYS にも含まれない persona key は通常通り書ける.

    0.5.26 で role / identity / address_user 等の core 9 属性が
    PROTECTED_KEYS に追加された。 それら以外の persona key (例:
    tone_correction のような会話で生成される振る舞い指示) は引き続き
    通常通り insert される.
    """
    user_fact = FactCandidate("persona", "tone_correction", "敬語をやめてほしい", 8)
    result = apply_candidate(db, user_fact, match=None, embedding=[0.1] * 768)
    assert result == "insert"


def test_apply_candidate_rejects_persona_core_identity(db):
    """0.5.26 回帰: persona/identity への write LLM 上書きを拒否する.
    (ソフィア事案で 14 段汚染された原因の塞ぎ)."""
    insert_new(db, FactCandidate("persona", "identity", "このペルソナの名前は『テスト』", 9), [0.1] * 768)
    db.commit()
    bad = FactCandidate("persona", "identity", "雑談の発話文章", 5)
    result = apply_candidate(db, bad, match=None, embedding=[0.5] * 768)
    assert result == "protected"
    rows = db.execute(
        "SELECT value, status FROM facts WHERE category='persona' AND key='identity' ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "このペルソナの名前は『テスト』"


def test_apply_candidate_rejects_persona_core_role(db):
    """0.5.26 回帰: persona/role への write LLM 上書きも拒否."""
    insert_new(db, FactCandidate("persona", "role", "バックエンド相棒", 9), [0.1] * 768)
    db.commit()
    bad = FactCandidate("persona", "role", "stitch の障害情報を検索", 5)
    result = apply_candidate(db, bad, match=None, embedding=[0.5] * 768)
    assert result == "protected"


def test_apply_candidate_rejects_persona_core_address_user(db):
    """0.5.26 回帰: persona/address_user への write LLM 上書きも拒否."""
    insert_new(db, FactCandidate("persona", "address_user", "マスター", 9), [0.1] * 768)
    db.commit()
    bad = FactCandidate("persona", "address_user", "inbox ってなんなの？", 5)
    result = apply_candidate(db, bad, match=None, embedding=[0.5] * 768)
    assert result == "protected"


def test_apply_candidate_allows_other_categories(db):
    """preference/skill/profile/context/aversion は完全に保護対象外."""
    fact = FactCandidate("preference", "coffee", "深煎り", 6)
    result = apply_candidate(db, fact, match=None, embedding=[0.1] * 768)
    assert result == "insert"
