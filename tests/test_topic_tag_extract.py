"""トピックタグ抽出 (scripts.topic.tag_extract) のテスト."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.topic.persist import save_tags
from scripts.topic.tag_extract import build_prompt, extract_tags, parse_tags


@dataclass
class FakeTagClient:
    response: str = "[]"
    embed_vec: list[float] = field(default_factory=lambda: [1.0] + [0.0] * 767)
    last_prompt: str = ""

    def generate(self, model, prompt, num_ctx=None):
        self.last_prompt = prompt
        return self.response

    def embed(self, model, text):
        return list(self.embed_vec)


def test_parse_tags_from_clean_array():
    assert parse_tags('["サイドバー UI", "メンション設計"]') == ["サイドバー UI", "メンション設計"]


def test_parse_tags_strips_code_fence():
    assert parse_tags('```json\n["X"]\n```') == ["X"]


def test_parse_tags_caps_at_three():
    assert parse_tags('["a","b","c","d","e"]') == ["a", "b", "c"]


def test_parse_tags_empty_on_invalid_json():
    assert parse_tags("not json") == []
    assert parse_tags("") == []


def test_parse_tags_drops_empty_strings():
    assert parse_tags('["", "x", " "]') == ["x"]


def test_parse_tags_truncates_long():
    long = "あ" * 50
    out = parse_tags(json.dumps([long]))
    assert len(out) == 1
    assert len(out[0]) <= 30


def test_extract_tags_returns_list():
    client = FakeTagClient(response='["Renju 命名"]')
    out = extract_tags("user", "Renju にしましょう", [], client)
    assert out == ["Renju 命名"]


def test_extract_tags_returns_empty_on_llm_error():
    @dataclass
    class CrashClient:
        def generate(self, m, p, num_ctx=None):
            raise RuntimeError("boom")
        def embed(self, m, t):
            return []
    out = extract_tags("user", "x", [], CrashClient())
    assert out == []


def test_build_prompt_includes_role_and_content():
    p = build_prompt("user", "メンション設計どうする?", [])
    assert "メンション設計どうする?" in p
    assert "user" in p


def test_save_tags_persists_rows_and_embeddings(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        conn.execute("INSERT INTO topics(id) VALUES ('t1')")
        conn.commit()
        client = FakeTagClient()
        n = save_tags(conn, "t1", ["サイドバー UI", "メンション設計"], client)
        assert n == 2
        rows = conn.execute(
            "SELECT tag FROM topic_tags WHERE topic_id='t1' ORDER BY id"
        ).fetchall()
        assert [r[0] for r in rows] == ["サイドバー UI", "メンション設計"]
        # embedding 行も入る
        n_emb = conn.execute(
            "SELECT COUNT(*) FROM topic_tag_embeddings"
        ).fetchone()[0]
        assert n_emb == 2
    finally:
        conn.close()


def test_save_tags_creates_topic_row_if_missing(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        client = FakeTagClient()
        n = save_tags(conn, "new-topic", ["X"], client)
        assert n == 1
        n_topic = conn.execute(
            "SELECT COUNT(*) FROM topics WHERE id='new-topic'"
        ).fetchone()[0]
        assert n_topic == 1
    finally:
        conn.close()


def test_save_tags_skips_when_no_tags(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    try:
        client = FakeTagClient()
        n = save_tags(conn, "t1", [], client)
        assert n == 0
    finally:
        conn.close()
