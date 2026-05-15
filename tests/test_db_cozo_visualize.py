"""scripts.db_cozo.visualize のテスト.

通すためのテストではなく, build_graph が discussion_node + edge を正しく集めて
JSON 化できるかの境界確認.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.discussion import add_edge, add_node
from scripts.db_cozo.repo import ensure_topic, save_episode
from scripts.db_cozo.visualize import build_graph, render_html


@pytest.fixture
def client(tmp_path: Path):
    return init_db(tmp_path / "p.cozo.db")


@pytest.fixture
def db_path(tmp_path: Path):
    return tmp_path / "p.cozo.db"


def _seed_renju(client) -> tuple[int, int, int]:
    """Renju 議論: option -> decision -> retraction の chain を作る."""
    ensure_topic(client, "t-renju")
    e1 = save_episode(
        client, role="user", content="2 カラム案を検討", session_id="s1",
        topic_id="t-renju",
    )
    e2 = save_episode(
        client, role="user", content="2 カラム採用", session_id="s1",
        topic_id="t-renju",
    )
    e3 = save_episode(
        client, role="user", content="やっぱり 1 カラム", session_id="s2",
        topic_id="t-renju",
    )
    n1 = add_node(client, kind="option", title="2 カラム案",
                  state="proposed", episode_id=e1)
    n2 = add_node(client, kind="decision", title="2 カラム採用",
                  state="accepted", episode_id=e2)
    n3 = add_node(client, kind="retraction", title="2 カラム撤回",
                  state="accepted", episode_id=e3)
    add_edge(client, n1, n2, "決定")
    add_edge(client, n2, n3, "撤回")
    return n1, n2, n3


def test_build_graph_collects_nodes_and_edges(db_path):
    client = init_db(db_path)
    n1, n2, n3 = _seed_renju(client)
    g = build_graph(db_path)
    assert g["meta"]["node_count"] == 3
    assert g["meta"]["edge_count"] == 2
    ids = {n["id"] for n in g["nodes"]}
    assert ids == {n1, n2, n3}
    relations = {l["kind"] for l in g["links"]}
    assert relations == {"決定", "撤回"}


def test_build_graph_attaches_topic_id_via_episode(db_path):
    client = init_db(db_path)
    n1, _, _ = _seed_renju(client)
    g = build_graph(db_path)
    topics = {n["topic_id"] for n in g["nodes"]}
    # 全 node が Renju episode 経由なので t-renju に紐付く
    assert topics == {"t-renju"}
    assert any(t["id"] == "t-renju" for t in g["topics"])


def test_build_graph_topic_filter(db_path):
    client = init_db(db_path)
    _seed_renju(client)
    # 別 topic も足す
    ensure_topic(client, "t-coffee")
    e_other = save_episode(
        client, role="user", content="コーヒー深煎り", session_id="s3",
        topic_id="t-coffee",
    )
    add_node(client, kind="topic", title="コーヒー", episode_id=e_other)

    g = build_graph(db_path, topic_id="t-renju")
    topics = {n["topic_id"] for n in g["nodes"]}
    assert topics == {"t-renju"}
    assert g["meta"]["node_count"] == 3


def test_build_graph_limit_keeps_latest(db_path):
    client = init_db(db_path)
    n1, n2, n3 = _seed_renju(client)
    g = build_graph(db_path, limit=2)
    ids = [n["id"] for n in g["nodes"]]
    # 最新 2 件 = n2, n3
    assert ids == [n2, n3]


def test_build_graph_edges_restricted_to_visible_nodes(db_path):
    client = init_db(db_path)
    n1, n2, n3 = _seed_renju(client)
    # limit=2 で n1 が落ちると、 n1->n2 の「決定」 edge は除外される
    g = build_graph(db_path, limit=2)
    assert g["meta"]["edge_count"] == 1
    assert g["links"][0]["kind"] == "撤回"


def test_render_html_embeds_data(db_path):
    client = init_db(db_path)
    _seed_renju(client)
    g = build_graph(db_path)
    html = render_html(g)
    assert "__GRAPH_DATA_JSON__" not in html  # 置換済
    assert '"nodes"' in html
    assert "2 カラム" in html


def test_render_html_with_custom_template(db_path):
    client = init_db(db_path)
    _seed_renju(client)
    g = build_graph(db_path)
    html = render_html(g, template="<x>__GRAPH_DATA_JSON__</x>")
    payload = json.loads(html[3:-4])
    assert payload["meta"]["node_count"] == 3


def test_build_graph_empty_db(db_path):
    init_db(db_path)
    g = build_graph(db_path)
    assert g["meta"]["node_count"] == 0
    assert g["meta"]["edge_count"] == 0
    assert g["nodes"] == []
    assert g["links"] == []
