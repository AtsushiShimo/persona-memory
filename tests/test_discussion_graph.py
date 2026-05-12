"""議論グラフ (Discussion Graph) Phase A1 の smoke test (0.6.13).

低レベル CRUD + 状態遷移 + 末端 accepted decision の即答 traversal を検証.
write LLM 統合 (Phase A2) / recall 組み込み (Phase B) はまだ存在しないため
テスト対象外.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.repo import save_episode
from scripts.discussion.graph import (
    add_edge,
    add_node,
    latest_decision_for_topic,
    nearest_discussion_nodes,
    neighbors,
    supersede_node,
    transition_state,
)
from scripts.shared.embedding import pack
from scripts.db.migrate import init_db


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "p.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn
    conn.close()


def test_add_node_returns_id_and_persists(db):
    node_id = add_node(db, kind="topic", title="Renju の最終決定")
    assert node_id > 0
    row = db.execute(
        "SELECT kind, title, state FROM discussion_nodes WHERE id=?",
        (node_id,),
    ).fetchone()
    assert row == ("topic", "Renju の最終決定", "proposed")


def test_invalid_kind_rejected(db):
    with pytest.raises(ValueError):
        add_node(db, kind="garbage", title="x")


def test_invalid_state_rejected(db):
    with pytest.raises(ValueError):
        add_node(db, kind="topic", title="x", state="zz")


def test_add_edge_links_two_nodes(db):
    t = add_node(db, kind="topic", title="X")
    o = add_node(db, kind="option", title="案 A")
    eid = add_edge(db, src_id=t, dst_id=o, edge_kind="considers")
    assert eid > 0
    row = db.execute(
        "SELECT src_id, dst_id, edge_kind FROM discussion_edges WHERE id=?",
        (eid,),
    ).fetchone()
    assert row == (t, o, "considers")


def test_invalid_edge_kind_rejected(db):
    t = add_node(db, kind="topic", title="X")
    o = add_node(db, kind="option", title="A")
    with pytest.raises(ValueError):
        add_edge(db, src_id=t, dst_id=o, edge_kind="garbage")


def test_transition_state(db):
    n = add_node(db, kind="decision", title="採用 A")
    transition_state(db, n, "accepted")
    s = db.execute(
        "SELECT state FROM discussion_nodes WHERE id=?", (n,),
    ).fetchone()[0]
    assert s == "accepted"


def test_supersede_node_demotes_old_and_links(db):
    old = add_node(db, kind="decision", title="採用 A", state="accepted")
    new = add_node(db, kind="decision", title="採用 B", state="accepted")
    supersede_node(db, old_id=old, new_id=new)

    old_state = db.execute(
        "SELECT state FROM discussion_nodes WHERE id=?", (old,),
    ).fetchone()[0]
    assert old_state == "superseded"

    edge = db.execute(
        "SELECT src_id, dst_id, edge_kind FROM discussion_edges "
        "WHERE src_id=? AND dst_id=?",
        (new, old),
    ).fetchone()
    assert edge == (new, old, "supersedes")


def test_latest_decision_for_topic_returns_accepted(db):
    """topic → considers → option → decides → decision(accepted) のパス即答."""
    topic = add_node(db, kind="topic", title="Renju カテゴリ作成方法")
    opt_a = add_node(db, kind="option", title="テンプレ提供")
    opt_b = add_node(db, kind="option", title="自由作成")
    add_edge(db, src_id=topic, dst_id=opt_a, edge_kind="considers")
    add_edge(db, src_id=topic, dst_id=opt_b, edge_kind="considers")

    # opt_b を採用、 opt_a は却下
    dec = add_node(db, kind="decision", title="自由作成で決定", state="accepted")
    add_edge(db, src_id=opt_b, dst_id=dec, edge_kind="decides")

    out = latest_decision_for_topic(db, topic)
    assert out is not None
    assert out["title"] == "自由作成で決定"
    assert out["state"] == "accepted"


def test_latest_decision_returns_none_when_unresolved(db):
    """accepted decision が無い topic は None を返す."""
    topic = add_node(db, kind="topic", title="未決")
    opt = add_node(db, kind="option", title="案 A")
    add_edge(db, src_id=topic, dst_id=opt, edge_kind="considers")
    # decide エッジを張らない
    assert latest_decision_for_topic(db, topic) is None


def test_neighbors_returns_both_directions(db):
    t = add_node(db, kind="topic", title="T")
    o = add_node(db, kind="option", title="O")
    r = add_node(db, kind="rationale", title="R")
    add_edge(db, src_id=t, dst_id=o, edge_kind="considers")
    add_edge(db, src_id=r, dst_id=t, edge_kind="depends_on")

    out = neighbors(db, t)
    titles = {n["title"]: n for n in out}
    assert "O" in titles and "R" in titles
    assert titles["O"]["direction"] == "out"
    assert titles["R"]["direction"] == "in"


def test_neighbors_filters_by_edge_kind(db):
    t = add_node(db, kind="topic", title="T")
    o = add_node(db, kind="option", title="O")
    r = add_node(db, kind="rationale", title="R")
    add_edge(db, src_id=t, dst_id=o, edge_kind="considers")
    add_edge(db, src_id=t, dst_id=r, edge_kind="depends_on")

    out = neighbors(db, t, edge_kinds=["considers"])
    titles = [n["title"] for n in out]
    assert titles == ["O"]


def test_node_can_link_to_episode(db):
    eid = save_episode(db, role="user", content="Renju の議論", session_id="s1")
    n = add_node(db, kind="topic", title="Renju 設計", episode_id=eid)
    linked = db.execute(
        "SELECT episode_id FROM discussion_nodes WHERE id=?", (n,),
    ).fetchone()[0]
    assert linked == eid


# 0.6.18 Phase B: nearest_discussion_nodes (embedding 近傍検索)
def test_nearest_discussion_nodes_returns_closest_by_cosine(db):
    """完全一致 embedding が最近接になる + max_distance で遠いものを除外."""
    # 単純な 3 次元 embedding で挙動を確かめる
    near = pack([1.0, 0.0, 0.0])
    mid = pack([0.7, 0.7, 0.0])
    far = pack([0.0, 0.0, 1.0])
    n_near = add_node(db, kind="topic", title="Renju 直近", embedding=near)
    n_mid = add_node(db, kind="topic", title="関連あり", embedding=mid)
    add_node(db, kind="topic", title="無関係", embedding=far)
    add_node(db, kind="topic", title="埋め込み無し")  # embedding NULL

    out = nearest_discussion_nodes(db, [1.0, 0.0, 0.0], top_k=3, max_distance=0.6)
    # 完全一致 → mid (cos sim 0.7, dist 0.3) → far (dist 1.0) は閾値超え弾く
    # 埋め込み無しは候補外
    ids = [n["id"] for n in out]
    assert ids == [n_near, n_mid]
    assert out[0]["distance"] < 1e-6  # 完全一致


def test_nearest_discussion_nodes_filters_by_kind_and_state(db):
    near = pack([1.0, 0.0, 0.0])
    add_node(db, kind="topic", title="T", embedding=near)
    add_node(db, kind="decision", title="D-proposed", embedding=near, state="proposed")
    n_acc = add_node(db, kind="decision", title="D-accepted", embedding=near, state="accepted")

    out = nearest_discussion_nodes(
        db, [1.0, 0.0, 0.0], top_k=5,
        kinds=["decision"], states=["accepted"],
    )
    assert [n["id"] for n in out] == [n_acc]


def test_nearest_discussion_nodes_empty_query_returns_empty(db):
    add_node(db, kind="topic", title="X", embedding=pack([1.0, 0.0]))
    assert nearest_discussion_nodes(db, [], top_k=3) == []
