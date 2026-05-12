"""Renju 続き呼び戻し シナリオ end-to-end smoke (0.6.18 Phase B).

write → discussion_nodes 蓄積 → recall で「直近の議論」 が冒頭に出ることを
ストーリーで確認する. 0.6.17 リリース後にマスターが persona-test3 で
「Renju の続き始めようか」 と発話した時、 ソフィアが議論の止まった場所を
拾えなかった事象の回帰防止.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db.connection import connect
from scripts.db.migrate import init_db
from scripts.db.repo import save_episode
from scripts.discussion.backfill import backfill
from scripts.recall.run import recall
from scripts.write.run import process_episode


@dataclass
class ScenarioClient:
    """write LLM + recall LLM + embedder の統合 mock.

    - generate(): prompt の特徴語で分岐し、 write/recall の役割を判定.
    - embed(): キーワード辞書. 命中しなければ default ベクトル.
    """
    write_nodes: list[dict] = field(default_factory=list)
    write_facts: list[dict] = field(default_factory=list)
    recall_summary: str = "Renju の最後の議論は盤サイズ選定で止まっています。"
    recall_search_history: bool = True
    embedding_map: dict[str, list[float]] = field(default_factory=dict)
    default_vec: list[float] = field(default_factory=lambda: [1.0] + [0.0] * 767)

    def generate(self, model: str, prompt: str) -> str:
        # recall analyze (analyze_query) は 'search_history' を含むキー名でプロンプト
        if "search_history" in prompt:
            return json.dumps(
                {
                    "keywords": ["Renju", "盤サイズ"],
                    "search_history": self.recall_search_history,
                    "trigger_phrase": "前の議論",
                },
                ensure_ascii=False,
            )
        # write extract: {"facts": [...], "nodes": [...]} を返す
        return json.dumps(
            {"facts": self.write_facts, "nodes": self.write_nodes},
            ensure_ascii=False,
        )

    def embed(self, model: str, text: str) -> list[float]:
        for k, v in self.embedding_map.items():
            if k in text:
                return list(v)
        return list(self.default_vec)


@pytest.fixture
def db(tmp_path: Path):
    db_path = tmp_path / "renju.db"
    init_db(db_path)
    conn = connect(db_path)
    yield conn, db_path
    conn.close()


def test_renju_continuation_via_write_path(db):
    """write 経由でノードが入り、 recall で『直近の議論』 が拾える."""
    conn, db_path = db
    renju_vec = [0.0, 1.0] + [0.0] * 766

    # 1. 過去発話を episodes に保存 (ユーザーが Renju の議論を始めた発話)
    eid = save_episode(
        conn, role="user",
        content="Renju の盤サイズ、 15x15 と 19x19 のどっちにしようか",
        session_id="s1",
    )
    conn.commit()

    # 2. write 経由で process_episode → discussion_nodes に topic が入る
    client = ScenarioClient(
        write_nodes=[
            {
                "kind": "topic", "title": "Renju の盤サイズ選定",
                "state": "proposed",
                "content": "15x15 と 19x19 の比較で止まっている",
            },
        ],
        write_facts=[],
        embedding_map={"Renju": renju_vec, "盤サイズ": renju_vec},
        default_vec=renju_vec,
    )
    process_episode(conn, eid, buffer_n=3, client=client)

    # node が入って embedding も付いているか
    row = conn.execute(
        "SELECT title, embedding FROM discussion_nodes WHERE episode_id=?",
        (eid,),
    ).fetchone()
    assert row is not None
    assert "盤サイズ" in row[0]
    assert row[1] is not None and len(row[1]) > 0

    # 3. recall で「前の議論どうだった?」 と聞くと discussion ブロックが出る
    out = recall(conn, "前の Renju の続きどうする?", client)
    assert "## 直近の議論" in out
    assert "Renju の盤サイズ選定" in out


def test_renju_continuation_via_backfill(db):
    """0.6.17 以前に ingest 済み (discussion_nodes 無し) を backfill で救済."""
    conn, db_path = db
    renju_vec = [0.0, 1.0] + [0.0] * 766

    # 過去発話は episodes に既に有り (= 旧版で ingest 済) だが nodes 空の状態
    save_episode(
        conn, role="user",
        content="Renju の盤サイズで盤面決まらず",
        session_id="s1",
    )
    conn.commit()

    client = ScenarioClient(
        write_nodes=[
            {"kind": "decision", "title": "盤サイズ 15x15 で確定",
             "state": "accepted", "content": "最終結論"},
        ],
        embedding_map={"Renju": renju_vec, "盤サイズ": renju_vec, "15x15": renju_vec},
        default_vec=renju_vec,
    )
    counts = backfill(db_path, client=client)
    assert counts["nodes_added"] == 1

    # backfill 後は recall が同じ DB を開く → discussion_nodes hit する
    conn2 = connect(db_path)
    try:
        out = recall(conn2, "Renju の続きは?", client)
        assert "## 直近の議論" in out
        assert "盤サイズ 15x15 で確定" in out
    finally:
        conn2.close()


def test_renju_continuation_no_recall_when_topic_unrelated(db):
    """直近に Renju 議論が無く、 全く別の topic しか居ない場合は混入しない."""
    conn, db_path = db
    other_vec = [0.0] * 766 + [0.0, 1.0]  # query と直交

    save_episode(conn, role="user", content="昼食の話", session_id="s1")
    conn.commit()

    # 過去の議論は「お昼ご飯どうする」 等で Renju とは embedding が離れている
    from scripts.discussion.graph import add_node
    from scripts.shared.embedding import pack
    add_node(
        conn, kind="topic", title="昼食どうする",
        embedding=pack(other_vec),
    )
    conn.commit()

    client = ScenarioClient(
        write_nodes=[], recall_summary="",
        recall_search_history=False,
        embedding_map={"Renju": [1.0] + [0.0] * 767},
        default_vec=[1.0] + [0.0] * 767,
    )
    out = recall(conn, "Renju の盤サイズ?", client)
    assert "## 直近の議論" not in out
