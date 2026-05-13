"""scripts.db_cozo.discussion のテスト."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db_cozo.connection import EMBEDDING_DIM, init_db
from scripts.db_cozo.discussion import (
    add_edge,
    add_node,
    chain_from,
    find_terminal_nodes,
    get_last_node_in_topic,
    nearest_nodes,
)
from scripts.db_cozo.repo import ensure_topic, save_episode, set_active_topic


@pytest.fixture
def client(tmp_path: Path):
    return init_db(tmp_path / "p.cozo.db")


def _vec(seed: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[seed % EMBEDDING_DIM] = 1.0
    return v


def test_add_node_returns_int_id(client):
    nid = add_node(client, kind="topic", title="X")
    assert isinstance(nid, int)


def test_add_node_with_embedding_persists_and_searchable(client):
    nid = add_node(client, kind="topic", title="メンション設計",
                   embedding=_vec(7))
    hits = nearest_nodes(client, _vec(7), top_k=3, distance_max=0.1)
    assert hits and hits[0]["id"] == nid
    assert hits[0]["distance"] == pytest.approx(0.0, abs=1e-4)


def test_add_edge_creates_directed_link(client):
    n1 = add_node(client, kind="topic", title="A")
    n2 = add_node(client, kind="decision", title="B")
    add_edge(client, n1, n2, "決定")
    chain = chain_from(client, n1)
    assert [c["id"] for c in chain] == [n1, n2]


def test_chain_from_traverses_multi_hop(client):
    nodes = []
    for i, k in enumerate(["topic", "option", "decision", "observation"]):
        nodes.append(add_node(client, kind=k, title=f"step{i}"))
    add_edge(client, nodes[0], nodes[1], "検討")
    add_edge(client, nodes[1], nodes[2], "採用")
    add_edge(client, nodes[2], nodes[3], "次バトン")
    chain = chain_from(client, nodes[0])
    assert [c["title"] for c in chain] == ["step0", "step1", "step2", "step3"]


def test_find_terminal_nodes(client):
    nodes = []
    for i in range(4):
        nodes.append(add_node(client, kind="topic", title=f"t{i}"))
    add_edge(client, nodes[0], nodes[1], "x")
    add_edge(client, nodes[1], nodes[2], "x")
    add_edge(client, nodes[2], nodes[3], "x")
    terminals = find_terminal_nodes(client, nodes)
    assert len(terminals) == 1
    assert terminals[0]["id"] == nodes[3]


def test_find_terminal_nodes_multiple_branches(client):
    a = add_node(client, kind="topic", title="root")
    b = add_node(client, kind="topic", title="branchA")
    c = add_node(client, kind="topic", title="branchB")
    add_edge(client, a, b, "派生")
    add_edge(client, a, c, "派生")
    terminals = find_terminal_nodes(client, [a, b, c])
    ids = sorted(t["id"] for t in terminals)
    assert ids == sorted([b, c])


def test_get_last_node_in_topic(client):
    ensure_topic(client, "t1")
    set_active_topic(client, "s1", "t1")
    save_episode(client, role="user", content="x", session_id="s1")
    eid2 = save_episode(client, role="assistant", content="y", session_id="s1")
    add_node(client, kind="topic", title="A", episode_id=1)
    n2 = add_node(client, kind="decision", title="B", episode_id=eid2)
    last = get_last_node_in_topic(client, "t1")
    assert last["id"] == n2


def test_get_last_node_in_topic_returns_none_when_empty(client):
    assert get_last_node_in_topic(client, "missing") is None


def test_nearest_nodes_filters_by_distance(client):
    add_node(client, kind="topic", title="far", embedding=_vec(500))
    near = add_node(client, kind="topic", title="near", embedding=_vec(0))
    hits = nearest_nodes(client, _vec(0), top_k=5, distance_max=0.1)
    assert len(hits) == 1 and hits[0]["id"] == near
