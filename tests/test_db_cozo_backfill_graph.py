"""scripts.db_cozo.backfill_graph + graph_extract のテスト."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db_cozo.backfill_graph import backfill, backfill_one
from scripts.db_cozo.connection import init_db
from scripts.db_cozo.discussion import chain_from, find_terminal_nodes, get_last_node_in_topic
from scripts.db_cozo.graph_extract import (
    extract_node_with_relation, parse_node,
)
from scripts.db_cozo.repo import ensure_topic, save_episode, set_active_topic


@dataclass
class FakeGraphClient:
    """LLM 呼出 #N で出す回答のスクリプト. embed は固定 vector."""
    responses: list[str] = field(default_factory=list)
    embed_vec: list[float] = field(default_factory=lambda: [1.0] + [0.0] * 767)
    call_idx: int = 0

    def generate(self, model, prompt, num_ctx=None):
        if self.call_idx < len(self.responses):
            r = self.responses[self.call_idx]
            self.call_idx += 1
            return r
        return "null"

    def embed(self, model, text):
        return list(self.embed_vec)


# ── parse_node ──

def test_parse_node_returns_none_for_null():
    assert parse_node("null") is None
    assert parse_node("") is None


def test_parse_node_parses_minimum_fields():
    nc = parse_node('{"kind": "decision", "title": "Renju 採用"}')
    assert nc.kind == "decision"
    assert nc.title == "Renju 採用"
    assert nc.state == "proposed"
    assert nc.prev_relation is None


def test_parse_node_with_prev_relation():
    nc = parse_node(
        '{"kind": "observation", "title": "次論点 4 つ提示", "prev_relation": "次バトン"}'
    )
    assert nc.prev_relation == "次バトン"


def test_parse_node_invalid_relation_becomes_none():
    nc = parse_node('{"kind": "topic", "title": "X", "prev_relation": "謎"}')
    assert nc.prev_relation is None


def test_parse_node_invalid_kind_returns_none():
    assert parse_node('{"kind": "weird", "title": "X"}') is None


def test_parse_node_caps_long_title():
    long = "あ" * 50
    nc = parse_node(json.dumps({"kind": "topic", "title": long}))
    assert nc is not None
    assert len(nc.title) <= 30


# ── backfill_one ──

@pytest.fixture
def client(tmp_path: Path):
    return init_db(tmp_path / "p.cozo.db")


def test_backfill_one_skips_short_user(client):
    ensure_topic(client, "t1")
    set_active_topic(client, "s1", "t1")
    save_episode(client, role="user", content="OK", session_id="s1")
    ep = {"id": 1, "role": "user", "content": "OK",
          "session_id": "s1", "topic_id": "t1"}
    fake = FakeGraphClient(responses=['{"kind": "topic", "title": "x"}'])
    nid, ek = backfill_one(client, ep, fake)
    assert nid is None and ek is None
    assert fake.call_idx == 0  # LLM 呼ばれていない


def test_backfill_one_creates_node_when_extracted(client):
    ensure_topic(client, "t1")
    set_active_topic(client, "s1", "t1")
    save_episode(
        client, role="assistant", content="Renju を採用しましょう ".ljust(100),
        session_id="s1",
    )
    ep = {"id": 1, "role": "assistant",
          "content": "Renju を採用しましょう ".ljust(100),
          "session_id": "s1", "topic_id": "t1"}
    fake = FakeGraphClient(responses=[
        '{"kind": "decision", "title": "Renju 採用", "state": "accepted"}'
    ])
    nid, ek = backfill_one(client, ep, fake)
    assert nid is not None
    assert ek is None  # prev_relation 無し


def test_backfill_one_adds_edge_with_prev_relation(client):
    ensure_topic(client, "t1")
    set_active_topic(client, "s1", "t1")
    e1 = save_episode(client, role="assistant",
                      content="A " + "a" * 100, session_id="s1")
    e2 = save_episode(client, role="assistant",
                      content="B " + "b" * 100, session_id="s1")
    fake = FakeGraphClient(responses=[
        '{"kind": "topic", "title": "前提"}',
        '{"kind": "decision", "title": "採用", "state": "accepted", "prev_relation": "決定"}',
    ])
    backfill_one(client, {"id": e1, "role": "assistant",
                          "content": "A " + "a" * 100,
                          "session_id": "s1", "topic_id": "t1"}, fake)
    backfill_one(client, {"id": e2, "role": "assistant",
                          "content": "B " + "b" * 100,
                          "session_id": "s1", "topic_id": "t1"}, fake)
    res = client.run("?[from_id, to_id, kind] := *discussion_edge{from_id, to_id, kind}")
    assert len(res["rows"]) == 1
    assert res["rows"][0][2] == "決定"


def test_backfill_assigns_topic_id_for_legacy_episode(client):
    """旧 episodes (topic_id NULL) は session_id を topic_id に救済."""
    save_episode(
        client, role="assistant", content="x" * 100, session_id="legacy-sess",
    )
    # save_episode は環境変数で topic_id 自動設定するので、 NULL を強制
    client.run(
        "?[id, role, content, session_id, timestamp] := "
        "*episode{id, role, content, session_id, timestamp} "
        ":put episode {id => role, content, session_id, timestamp}"
    )
    ep = {"id": 1, "role": "assistant", "content": "x" * 100,
          "session_id": "legacy-sess", "topic_id": None}
    fake = FakeGraphClient(responses=['{"kind": "topic", "title": "rescue"}'])
    backfill_one(client, ep, fake)
    res = client.run("?[topic_id] := *episode{id: 1, topic_id}")
    assert res["rows"][0][0] == "legacy-sess"


# ── backfill (orchestrator) end-to-end ──

def test_backfill_renju_scenario_reproduces_chain(client, tmp_path: Path):
    """4 episodes で Renju 流れを再現. 末端 = 「次論点 4 つ」 が出るか."""
    ensure_topic(client, "t-renju")
    set_active_topic(client, "s1", "t-renju")
    contents = [
        "プロダクト名を決めましょう " + "a" * 100,
        "Renju を採用します " + "b" * 100,
        "カテゴリは自由作成で確定 " + "c" * 100,
        "次論点 4 つを提示します " + "d" * 100,
    ]
    for c in contents:
        save_episode(client, role="assistant", content=c, session_id="s1")

    fake = FakeGraphClient(responses=[
        '{"kind": "topic", "title": "プロダクト名"}',
        '{"kind": "decision", "title": "Renju 採用", "state": "accepted", "prev_relation": "決定"}',
        '{"kind": "decision", "title": "カテゴリ自由作成", "state": "accepted", "prev_relation": "次へ"}',
        '{"kind": "observation", "title": "次論点 4 つ提示", "prev_relation": "次バトン"}',
    ])
    # 「次へ」 は VALID_RELATIONS に無いので edge は作られないはず. 「派生」 で再試
    fake.responses[2] = (
        '{"kind": "decision", "title": "カテゴリ自由作成", "state": "accepted", "prev_relation": "派生"}'
    )

    result = backfill(tmp_path / "p.cozo.db", llm=fake, progress=False)
    # 上の DB と client は同じファイルではないため, fresh client で確認
    from scripts.db_cozo.connection import init_db as _init
    c2 = _init(tmp_path / "p.cozo.db")
    nodes = c2.run(
        "?[id, kind, title] := *discussion_node{id, kind, title} :order id"
    )["rows"]
    edges = c2.run(
        "?[from_id, to_id, kind] := *discussion_edge{from_id, to_id, kind}"
    )["rows"]
    assert len(nodes) == 4
    assert len(edges) == 3  # 4 nodes 間で 3 edges (派生 / 派生 / 次バトン)
    # 末端は #4 (次論点 4 つ提示)
    terminals = find_terminal_nodes(c2, [n[0] for n in nodes])
    assert len(terminals) == 1
    assert terminals[0]["title"] == "次論点 4 つ提示"
