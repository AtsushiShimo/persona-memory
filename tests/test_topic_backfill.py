"""scripts.topic.backfill_tags のテスト."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.topic.backfill_tags import (
    _episodes_to_process,
    backfill,
    backfill_one,
    count_pending_episodes,
)


@dataclass
class FakeBackfillClient:
    response: str = "[]"
    embed_vec: list[float] = field(default_factory=lambda: [1.0] + [0.0] * 767)
    generate_calls: int = 0

    def generate(self, model, prompt, num_ctx=None):
        self.generate_calls += 1
        return self.response

    def embed(self, model, text):
        return list(self.embed_vec)


@pytest.fixture
def db_path(tmp_path: Path):
    p = tmp_path / "p.db"
    init_db(p)
    return p


def _seed_episodes(db_path: Path, episodes: list[dict]) -> None:
    """直接 INSERT (topic_id 制御を確実にするため). monkeypatch で TOPIC_DISABLE 不要."""
    conn = connect(db_path)
    try:
        for e in episodes:
            conn.execute(
                "INSERT INTO episodes(role, content, session_id, topic_id) "
                "VALUES (?,?,?,?)",
                (e["role"], e["content"], e["session_id"], e.get("topic_id")),
            )
        conn.commit()
    finally:
        conn.close()


def test_episodes_to_process_returns_only_unprocessed(db_path: Path):
    """topic_tags が無い episode のみ対象."""
    _seed_episodes(db_path, [
        {"role": "user", "content": "X" * 100, "session_id": "s1"},
        {"role": "assistant", "content": "Y" * 100, "session_id": "s1"},
    ])
    conn = connect(db_path)
    try:
        eps = _episodes_to_process(conn, since_episode_id=None)
        assert len(eps) == 2
    finally:
        conn.close()


def test_short_user_episodes_are_skipped(db_path: Path):
    """短文 user 発話は LLM 節約のため事前 skip."""
    _seed_episodes(db_path, [
        {"role": "user", "content": "OK", "session_id": "s1"},
        {"role": "user", "content": "X" * 100, "session_id": "s1"},
    ])
    conn = connect(db_path)
    try:
        eps = _episodes_to_process(conn, None, apply_short_skip=True)
        assert len(eps) == 1
        # 反対に skip 解除すれば 2 件
        eps2 = _episodes_to_process(conn, None, apply_short_skip=False)
        assert len(eps2) == 2
    finally:
        conn.close()


def test_backfill_one_assigns_topic_id_from_session_when_null(db_path: Path):
    _seed_episodes(db_path, [
        {"role": "user", "content": "Renju のサイドバーを設計しよう", "session_id": "sess-A"},
    ])
    conn = connect(db_path)
    client = FakeBackfillClient(response='["サイドバー設計"]')
    try:
        eps = _episodes_to_process(conn, None, apply_short_skip=False)
        topic_id, n = backfill_one(conn, eps[0], client)
        assert topic_id == "sess-A"
        assert n == 1
        # episode に topic_id が後付けされる
        row = conn.execute(
            "SELECT topic_id FROM episodes WHERE id=?", (eps[0]["id"],)
        ).fetchone()
        assert row[0] == "sess-A"
        # topic_tags に保存
        n_tags = conn.execute(
            "SELECT COUNT(*) FROM topic_tags WHERE topic_id='sess-A'"
        ).fetchone()[0]
        assert n_tags == 1
    finally:
        conn.close()


def test_backfill_one_uses_fixed_topic_id_when_given(db_path: Path):
    _seed_episodes(db_path, [
        {"role": "user", "content": "X" * 100, "session_id": "sess-A"},
    ])
    conn = connect(db_path)
    client = FakeBackfillClient(response='["X"]')
    try:
        eps = _episodes_to_process(conn, None, apply_short_skip=False)
        topic_id, n = backfill_one(conn, eps[0], client, fixed_topic_id="global")
        assert topic_id == "global"
        # episode の topic_id は session_id ではなく固定値
        row = conn.execute(
            "SELECT topic_id FROM episodes WHERE id=?", (eps[0]["id"],)
        ).fetchone()
        assert row[0] == "global"
    finally:
        conn.close()


def test_backfill_one_dry_run_does_not_write(db_path: Path):
    _seed_episodes(db_path, [
        {"role": "user", "content": "Renju 設計の続き", "session_id": "s1"},
    ])
    conn = connect(db_path)
    client = FakeBackfillClient(response='["Renju 設計"]')
    try:
        eps = _episodes_to_process(conn, None, apply_short_skip=False)
        topic_id, n = backfill_one(conn, eps[0], client, dry_run=True)
        assert n == 1
        n_saved = conn.execute("SELECT COUNT(*) FROM topic_tags").fetchone()[0]
        assert n_saved == 0
    finally:
        conn.close()


def test_backfill_one_skips_when_no_tags_extracted(db_path: Path):
    _seed_episodes(db_path, [
        {"role": "assistant", "content": "了解です. ".ljust(100), "session_id": "s1"},
    ])
    conn = connect(db_path)
    client = FakeBackfillClient(response="[]")
    try:
        eps = _episodes_to_process(conn, None, apply_short_skip=False)
        topic_id, n = backfill_one(conn, eps[0], client)
        assert n == 0
        n_saved = conn.execute("SELECT COUNT(*) FROM topic_tags").fetchone()[0]
        assert n_saved == 0
    finally:
        conn.close()


def test_backfill_processes_multiple_with_limit(db_path: Path):
    _seed_episodes(db_path, [
        {"role": "user", "content": "X" * 100, "session_id": f"s{i}"}
        for i in range(5)
    ])
    client = FakeBackfillClient(response='["tag"]')
    result = backfill(db_path, client=client, limit=3)
    assert result["episodes_seen"] == 3
    assert result["tags_added"] == 3


def test_count_pending_episodes_returns_eligible_count(db_path: Path):
    _seed_episodes(db_path, [
        {"role": "user", "content": "OK", "session_id": "s1"},  # short skip
        {"role": "user", "content": "X" * 100, "session_id": "s2"},
        {"role": "assistant", "content": "Y" * 50, "session_id": "s3"},
    ])
    n = count_pending_episodes(db_path)
    assert n == 2
