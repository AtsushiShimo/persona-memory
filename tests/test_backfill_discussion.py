"""discussion_nodes 遡及抽出 (0.6.18 Phase B) のテスト."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode
from scripts.discussion.backfill import backfill, backfill_one


@dataclass
class FakeWriteClient:
    """write LLM の mock. generate() で JSON 配列を返し、embed() は固定 vector."""
    nodes_payload: list[dict] = field(default_factory=list)
    facts_payload: list[dict] = field(default_factory=list)
    embed_vec: list[float] = field(default_factory=lambda: [1.0] + [0.0] * 767)
    generate_calls: int = 0

    def generate(self, model: str, prompt: str, num_ctx: int | None = None) -> str:
        self.generate_calls += 1
        # 0.6.17 以降の出力形式: {"facts": [...], "nodes": [...]}
        return json.dumps(
            {"facts": self.facts_payload, "nodes": self.nodes_payload},
            ensure_ascii=False,
        )

    def embed(self, model: str, text: str) -> list[float]:
        return list(self.embed_vec)


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn, db_path
    conn.close()


def test_backfill_one_processes_episode_unconditionally(db):
    """backfill_one は orchestrator (= backfill) の対象選定とは独立に動く primitive.
    既存 node の有無はチェックせず、 与えられた episode を素直に処理する."""
    conn, _ = db
    eid = save_episode(conn, role="user", content="Renju の盤サイズで止まっている", session_id="s1")
    from scripts.discussion.graph import add_node
    add_node(conn, kind="topic", title="既存", episode_id=eid)
    conn.commit()

    client = FakeWriteClient(nodes_payload=[
        {"kind": "topic", "title": "新規 topic", "state": "proposed"},
    ])
    n = backfill_one(conn, {"id": eid, "role": "user", "content": "x", "session_id": "s1"}, client)
    assert n == 1  # 既存 node は無視して新規追加


def test_backfill_skips_when_listed_episodes_already_have_nodes(db):
    conn, db_path = db
    eid = save_episode(conn, role="user", content="Renju 議論", session_id="s1")
    from scripts.discussion.graph import add_node
    add_node(conn, kind="topic", title="既存", episode_id=eid)
    conn.commit()

    counts = backfill(db_path, client=FakeWriteClient(nodes_payload=[
        {"kind": "topic", "title": "新規", "state": "proposed"},
    ]))
    assert counts["episodes_seen"] == 0
    assert counts["nodes_added"] == 0


def test_backfill_inserts_nodes_with_embedding(db):
    conn, db_path = db
    save_episode(conn, role="user", content="Renju の盤サイズで止まっている", session_id="s1")
    conn.commit()

    client = FakeWriteClient(nodes_payload=[
        {"kind": "topic", "title": "Renju 盤サイズ",
         "state": "proposed", "content": "15x15 と 19x19 を比較"},
    ])
    counts = backfill(db_path, client=client, apply_short_skip=False)
    assert counts["episodes_seen"] == 1
    assert counts["episodes_with_nodes"] == 1
    assert counts["nodes_added"] == 1

    conn2 = connect(db_path)
    try:
        row = conn2.execute(
            "SELECT kind, title, content, episode_id, embedding FROM discussion_nodes",
        ).fetchone()
        assert row[0] == "topic"
        assert row[1] == "Renju 盤サイズ"
        assert row[2] == "15x15 と 19x19 を比較"
        assert row[3] is not None  # episode_id linked
        assert row[4] is not None and len(row[4]) > 0  # embedding 入っている
    finally:
        conn2.close()


def test_backfill_does_not_touch_facts(db):
    conn, db_path = db
    save_episode(conn, role="user", content="コーヒーは深煎り好き", session_id="s1")
    conn.commit()

    # FakeWriteClient が facts も返すが、 backfill は無視する筈
    client = FakeWriteClient(
        facts_payload=[
            {"category": "preference", "key": "coffee", "value": "深煎り", "importance": 6},
        ],
        nodes_payload=[
            {"kind": "topic", "title": "コーヒーの好み", "state": "proposed"},
        ],
    )
    backfill(db_path, client=client, apply_short_skip=False)

    conn2 = connect(db_path)
    try:
        fc = conn2.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        nc = conn2.execute("SELECT COUNT(*) FROM discussion_nodes").fetchone()[0]
        assert fc == 0  # facts は無変更
        assert nc == 1  # nodes は挿入
    finally:
        conn2.close()


def test_backfill_dry_run_writes_nothing(db, capsys):
    conn, db_path = db
    save_episode(conn, role="user", content="Renju 議論", session_id="s1")
    conn.commit()

    client = FakeWriteClient(nodes_payload=[
        {"kind": "topic", "title": "T1", "state": "proposed"},
        {"kind": "option", "title": "案 A", "state": "proposed"},
    ])
    counts = backfill(db_path, dry_run=True, client=client, apply_short_skip=False)
    # dry_run でも 件数は数える
    assert counts["nodes_added"] == 2

    conn2 = connect(db_path)
    try:
        n = conn2.execute("SELECT COUNT(*) FROM discussion_nodes").fetchone()[0]
        assert n == 0  # 実際の書き込みは無い
    finally:
        conn2.close()


def test_backfill_skips_invalid_nodes(db):
    conn, db_path = db
    save_episode(conn, role="user", content="x", session_id="s1")
    conn.commit()

    client = FakeWriteClient(nodes_payload=[
        {"kind": "invalid_kind", "title": "X", "state": "proposed"},
        {"kind": "topic", "title": "OK", "state": "proposed"},
    ])
    counts = backfill(db_path, client=client, apply_short_skip=False)
    # parse_nodes が invalid_kind を弾く設計なら 1, 通すなら is_valid で 1.
    # いずれにせよ最終 add_node は valid なノードだけ.
    conn2 = connect(db_path)
    try:
        titles = [r[0] for r in conn2.execute("SELECT title FROM discussion_nodes")]
        assert "OK" in titles
        assert "X" not in titles
    finally:
        conn2.close()


# ── 0.6.20 軽量モデル + 短文 skip ─────────────────────────────────────

def test_short_user_episode_is_skipped_without_llm_call(db):
    """短文 user 発話は LLM を呼ばずに飛ばす (= generate_calls 増えない)."""
    conn, db_path = db
    save_episode(conn, role="user", content="OK", session_id="s1")  # 2 字
    save_episode(conn, role="user", content="ありがとう", session_id="s1")  # 5 字
    conn.commit()

    client = FakeWriteClient(nodes_payload=[
        {"kind": "topic", "title": "T", "state": "proposed"},
    ])
    counts = backfill(db_path, client=client)  # default = apply_short_skip
    assert client.generate_calls == 0  # LLM 呼ばれない
    assert counts["episodes_seen"] == 0


def test_assistant_short_episode_is_not_skipped(db):
    """assistant の短文は短い結論候補なので skip しない."""
    conn, db_path = db
    save_episode(conn, role="assistant", content="採用", session_id="s1")  # 2 字
    conn.commit()

    client = FakeWriteClient(nodes_payload=[
        {"kind": "decision", "title": "採用", "state": "accepted"},
    ])
    counts = backfill(db_path, client=client)
    assert client.generate_calls == 1
    assert counts["nodes_added"] == 1


def test_no_short_skip_processes_short_user_episodes(db):
    """apply_short_skip=False で 短文 user も処理対象に戻る."""
    conn, db_path = db
    save_episode(conn, role="user", content="OK", session_id="s1")
    conn.commit()

    client = FakeWriteClient(nodes_payload=[])  # 抽出 0 件想定
    counts = backfill(db_path, client=client, apply_short_skip=False)
    assert client.generate_calls == 1
    assert counts["episodes_seen"] == 1


def test_model_argument_is_propagated_to_generate(db, monkeypatch):
    """--model で指定したモデル名が generate() に渡る."""
    conn, db_path = db
    save_episode(
        conn, role="user", content="盤サイズの議論を続けたい", session_id="s1",
    )
    conn.commit()

    seen_models: list[str] = []
    client = FakeWriteClient(nodes_payload=[
        {"kind": "topic", "title": "盤サイズ", "state": "proposed"},
    ])
    orig_generate = client.generate

    def spy(model: str, prompt: str, num_ctx: int | None = None) -> str:
        seen_models.append(model)
        return orig_generate(model, prompt, num_ctx=num_ctx)
    client.generate = spy  # type: ignore[method-assign]

    backfill(db_path, client=client, model="gemma3:4b", apply_short_skip=False)
    assert seen_models == ["gemma3:4b"]
