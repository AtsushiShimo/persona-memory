"""lesson + trigger の CRUD + マッチング (scripts.reflection.lesson) のテスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db_cozo.connection import init_db
from scripts.reflection.lesson import (
    delete_triggers_for, format_lesson_block_for_prompt,
    format_tool_block_message, get_lesson_by_key, get_triggers_for,
    list_active_lessons, list_all_triggers, match_lessons_for_prompt,
    match_lessons_for_tool_call, register_trigger,
)
from scripts.reflection.seed_past_lessons import seed_all


@pytest.fixture
def db_path(tmp_path: Path):
    p = tmp_path / "p.cozo.db"
    init_db(p)  # schema
    return p


@pytest.fixture
def client(db_path):
    return init_db(db_path)


@pytest.fixture
def seeded_client(db_path):
    """全 PAST_LESSONS を投入済の client."""
    seed_all(db_path)
    return init_db(db_path)


def test_seed_all_writes_11_lessons(db_path):
    result = seed_all(db_path)
    assert result["lessons_written"] == 11
    assert result["triggers_registered"] > 0


def test_seed_is_idempotent(db_path):
    r1 = seed_all(db_path)
    r2 = seed_all(db_path)
    assert r1 == r2


def test_list_active_lessons_after_seed(seeded_client):
    lessons = list_active_lessons(seeded_client)
    assert len(lessons) == 11
    keys = {l.key for l in lessons}
    assert "cache_edit_block" in keys
    assert "no_partial_completion_report" in keys


def test_get_lesson_by_key(seeded_client):
    l = get_lesson_by_key(seeded_client, "cache_edit_block")
    assert l is not None
    assert "キャッシュ" in l.value
    assert l.importance == 10


def test_get_triggers_for_cache_edit(seeded_client):
    l = get_lesson_by_key(seeded_client, "cache_edit_block")
    trigs = get_triggers_for(seeded_client, l.fact_id)
    assert len(trigs) >= 2
    kinds = {t.kind for t in trigs}
    assert "path_edit" in kinds


def test_match_lesson_for_cache_edit_blocks(seeded_client):
    matches = match_lessons_for_tool_call(
        seeded_client, "Edit",
        {"file_path": "/Users/x/.claude/plugins/cache/persona-memory/foo.py"},
    )
    assert len(matches) >= 1
    block_actions = [m for m in matches if m.trigger.action == "block"]
    assert len(block_actions) >= 1


def test_match_lesson_for_safe_path_no_match(seeded_client):
    matches = match_lessons_for_tool_call(
        seeded_client, "Edit",
        {"file_path": "/Users/x/Desktop/claude_dev/persona-memory/scripts/foo.py"},
    )
    # キャッシュ以外なら block hit しない
    block = [m for m in matches if m.trigger.action == "block"]
    assert len(block) == 0


def test_match_lesson_for_bash_warn(seeded_client):
    """git commit / push に対し warn 系トリガーが当たる (= self_test_before_report)."""
    matches = match_lessons_for_tool_call(
        seeded_client, "Bash",
        {"command": "git commit -m 'foo'"},
    )
    assert any(m.lesson.key == "self_test_before_report" for m in matches)


def test_match_lesson_for_prompt_intent(seeded_client):
    matches = match_lessons_for_prompt(
        seeded_client, "新機能のスキーマ設計を考えて",
    )
    assert any(m.lesson.key == "read_existing_source_before_design"
               for m in matches)


def test_register_trigger_returns_id(client):
    # ダミー lesson fact を入れて trigger を register
    from scripts.reflection.seed_past_lessons import _upsert_lesson_in_cozo, LessonSeed
    fid = _upsert_lesson_in_cozo(
        client, LessonSeed(key="x_test", value="v", importance=5),
    )
    tid = register_trigger(client, fid, "path_edit", r"/foo/", "block")
    assert tid >= 1
    trigs = get_triggers_for(client, fid)
    assert len(trigs) == 1
    assert trigs[0].pattern == r"/foo/"


def test_delete_triggers_for_clears_all(seeded_client):
    l = get_lesson_by_key(seeded_client, "cache_edit_block")
    before = len(get_triggers_for(seeded_client, l.fact_id))
    assert before > 0
    delete_triggers_for(seeded_client, l.fact_id)
    after = len(get_triggers_for(seeded_client, l.fact_id))
    assert after == 0


def test_invalid_regex_fallback_to_substring(client):
    from scripts.reflection.seed_past_lessons import (
        _upsert_lesson_in_cozo, LessonSeed,
    )
    fid = _upsert_lesson_in_cozo(
        client, LessonSeed(key="bad_re", value="v", importance=5),
    )
    # 不正な正規表現 (閉じてない括弧)
    register_trigger(client, fid, "bash_cmd", "rm -rf (", "warn")
    matches = match_lessons_for_tool_call(
        client, "Bash", {"command": "rm -rf ( foo"},
    )
    # 不正 re でも substring fallback で hit
    assert any(m.lesson.key == "bad_re" for m in matches)


def test_format_lesson_block_for_prompt_empty():
    assert format_lesson_block_for_prompt([]) == ""


def test_format_tool_block_message_includes_tool(seeded_client):
    matches = match_lessons_for_tool_call(
        seeded_client, "Edit",
        {"file_path": "/Users/x/.claude/plugins/cache/persona-memory/x.py"},
    )
    msg = format_tool_block_message(matches, "Edit")
    assert "Edit" in msg
    assert "中止" in msg


def test_list_all_triggers_after_seed(seeded_client):
    pairs = list_all_triggers(seeded_client)
    assert len(pairs) > 0
    # 全 trigger が active な lesson に紐付いている
    for lesson, trig in pairs:
        assert lesson.fact_id == trig.lesson_fact_id
