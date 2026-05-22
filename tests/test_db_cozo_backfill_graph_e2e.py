"""backfill_graph の統合 e2e テスト (0.8.7).

設計:
- 単体テスト (tests/test_db_cozo_backfill_graph.py) は identify_topic を test
  double に差し替えて backfill_one 本体の責務を孤立して検証する.
- 本ファイルは **LLM のみ fake**, identify_topic / topic_summary / graph_extract /
  add_node / add_edge / find_alive_topics_by_summary_emb 等の **全経路を本物のコードで** 走らせ、
  部品結合時の化学反応 (= 経路を繋いで初めて出る不整合) を検出する.

fake LLM は prompt 内容で分岐 (= identify_topic の verify / topic summary /
graph_extract の 3 経路にそれぞれ意味のあるレスポンスを返す).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.db_cozo.backfill_graph import backfill
from scripts.db_cozo.connection import init_db
from scripts.db_cozo.discussion import find_terminal_nodes
from scripts.db_cozo.repo import (
    ensure_topic, save_episode, set_active_topic,
)


# 分岐の目印 (各 prompt template に含まれるユニークな日本語)
_MARK_VERIFY = "新発話がこの話題の"
_MARK_SUMMARY_INIT = "何の話題"
_MARK_SUMMARY_UPDATE = "現在の要約"
_MARK_EXTRACT_NODE = "議論ノード"


@dataclass
class SmartFakeLLM:
    """prompt 内容で分岐し本物経路を破綻なく走らせる fake.

    - verify (identify_topic._verify_topic_match): 常に "match" を返す
      → 2 個目以降の episode が 1 個目と同じ topic に統合される.
    - summary 系: 短い deterministic 要約.
    - graph_extract (= 議論ノード抽出): node_queue から順に出す.
    - embed: 固定 vector. find_alive_topics_by_summary_emb で候補として拾われる.
    """
    node_queue: list[str] = field(default_factory=list)
    embed_vec: list[float] = field(
        default_factory=lambda: [1.0] + [0.0] * 767,
    )
    # 観測用カウンタ
    verify_calls: int = 0
    summary_calls: int = 0
    extract_calls: int = 0
    extract_idx: int = 0

    def generate(self, model, prompt, num_ctx=None):
        if _MARK_VERIFY in prompt:
            self.verify_calls += 1
            return json.dumps({"answer": "match", "reason": "test stub"})
        if _MARK_SUMMARY_INIT in prompt or _MARK_SUMMARY_UPDATE in prompt:
            self.summary_calls += 1
            return "テスト用要約"
        if _MARK_EXTRACT_NODE in prompt:
            self.extract_calls += 1
            if self.extract_idx < len(self.node_queue):
                r = self.node_queue[self.extract_idx]
                self.extract_idx += 1
                return r
            return "null"
        # 未知の prompt は static null (= LLM 失敗扱いで安全側に倒れる)
        return "null"

    def embed(self, model, text):
        return list(self.embed_vec)


def _save_assistant_episodes(client, session_id: str, contents: list[str]):
    """episode を順次保存 (本物の save_episode 経由, content を 100 文字超に膨らます)."""
    ids = []
    for c in contents:
        padded = c + "_" + "x" * max(0, 100 - len(c))
        ids.append(save_episode(
            client, role="assistant", content=padded, session_id=session_id,
        ))
    return ids


def test_e2e_4_episodes_one_topic_full_chain(tmp_path: Path):
    """4 episode を投入し、 identify_topic + graph_extract が本物で繋がる経路で
    node 4 つ + edge 3 つ + 末端 1 つが出来上がる.

    検証する化学反応:
    - identify_topic が 2 個目以降を 1 個目と同 topic に統合する (verify=match)
    - 各 episode で graph_extract が node を抽出 → add_node
    - prev_relation が指定された 2 個目以降は add_edge
    - episode.topic_id が backfill_one で identify_topic の結果に上書きされる
    - find_terminal_nodes で「最後の episode の node が末端」 と判定される
    """
    db = tmp_path / "e2e.cozo.db"
    client = init_db(db)
    ensure_topic(client, "boot-sess")
    set_active_topic(client, "boot-sess", "boot-sess")

    contents = [
        "プロダクト名 Renju を提案します",
        "Renju を採用することに決めました",
        "カテゴリは自由作成で確定とします",
        "次論点 4 つを提示します",
    ]
    eps = _save_assistant_episodes(client, "boot-sess", contents)
    assert len(eps) == 4

    # 1 個目は prev_relation 無し (= 同 topic 内に直前 node 無し), 以降 3 個は 派生 で繋ぐ
    fake = SmartFakeLLM(node_queue=[
        '{"kind": "topic", "title": "Renju 提案"}',
        '{"kind": "decision", "title": "Renju 採用", "state": "accepted", "prev_relation": "派生"}',
        '{"kind": "decision", "title": "カテゴリ自由作成", "state": "accepted", "prev_relation": "派生"}',
        '{"kind": "observation", "title": "次論点 4 つ提示", "prev_relation": "次バトン"}',
    ])

    result = backfill(db, llm=fake, progress=False)

    # backfill orchestrator が 4 episode を見たこと
    assert result["episodes_seen"] == 4
    assert result["nodes_added"] == 4, f"unexpected: {result}"
    assert result["edges_added"] == 3, f"unexpected: {result}"

    # 本物経路で identify_topic が verify を 3 回 (= 2,3,4 個目で生きてる topic と照合) 呼んだ
    assert fake.verify_calls >= 1, "identify_topic が verify を呼んでいない (= 統合経路が走っていない)"
    # graph_extract が 4 回呼ばれた
    assert fake.extract_calls == 4

    # 同一 topic に統合されたか: episode 全件の topic_id が 1 種類
    res = client.run(
        "?[topic_id] := *episode{topic_id}, topic_id != null",
    )
    topic_ids = sorted({r[0] for r in res["rows"]})
    assert len(topic_ids) == 1, f"expected single topic, got {topic_ids}"
    # 自動発行された alive-XXXX 形式であること (= 新方式の証跡)
    assert topic_ids[0].startswith("alive-"), \
        f"identify_topic の新方式 topic が出ていない: {topic_ids[0]}"

    # 4 個目の node が末端 (outgoing edge を持たない)
    c2 = init_db(db)
    nodes = c2.run(
        "?[id, kind, title] := *discussion_node{id, kind, title} :order id",
    )["rows"]
    assert len(nodes) == 4
    terminals = find_terminal_nodes(c2, [n[0] for n in nodes])
    assert len(terminals) == 1
    assert terminals[0]["title"] == "次論点 4 つ提示"


def test_e2e_llm_returns_null_falls_back_to_observation(tmp_path: Path):
    """LLM が null を返しても content >= 8 文字なら最終 fallback で observation
    node が作られる (graph_extract.py:245-256). 本物の retry + fallback 経路を確認.
    """
    db = tmp_path / "e2e2.cozo.db"
    client = init_db(db)
    _save_assistant_episodes(client, "sess-x", ["最初の発話", "二つ目の発話"])
    fake = SmartFakeLLM(node_queue=["null", "null"])
    result = backfill(db, llm=fake, progress=False)
    # null でも fallback で observation node が出来る (= 仕様)
    assert result["nodes_added"] == 2, f"fallback で node が出来ていない: {result}"
    # 1 個目は prev 無しなので edge 無し, 2 個目は fallback で "派生" が付くので edge 1
    assert result["edges_added"] == 1, f"unexpected: {result}"
    assert result["episodes_seen"] == 2
    # fallback で出来た node は kind=observation
    c2 = init_db(db)
    nodes = c2.run(
        "?[kind] := *discussion_node{kind} :order kind",
    )["rows"]
    assert all(r[0] == "observation" for r in nodes), \
        f"fallback 経路の kind 想定外: {nodes}"


def test_e2e_too_short_content_skips_node(tmp_path: Path):
    """8 文字未満 (= 真に短い相槌) で LLM null なら fallback も走らず node 無し.

    回帰防止: content 長による早期 return 経路.
    """
    db = tmp_path / "e2e2b.cozo.db"
    client = init_db(db)
    # assistant 発話で 8 文字未満 → graph_extract の retry 内で早期 None.
    # ただし backfill_one の _is_likely_empty は user の SHORT_USER_SKIP_CHARS (50)
    # で判定するので, assistant で 8 文字未満を狙う.
    save_episode(client, role="assistant", content="短い",
                 session_id="sess-y")
    fake = SmartFakeLLM(node_queue=["null"])
    result = backfill(db, llm=fake, progress=False)
    assert result["nodes_added"] == 0, f"短文で node が出来てしまった: {result}"


def test_e2e_legacy_episode_gets_alive_topic_id(tmp_path: Path):
    """topic_id NULL の legacy episode が identify_topic 経由で alive-XXX に上書き.

    旧 session_id 救済の仕様ではなく、 0.7.3 以降の新方式 (= 自動 topic 発行) が
    本物の経路で動くことを確認.
    """
    db = tmp_path / "e2e3.cozo.db"
    client = init_db(db)
    save_episode(
        client, role="assistant",
        content="legacy 発話" + "y" * 100, session_id="legacy",
    )
    # save_episode 後に topic_id を強制 NULL に (= 旧 schema からのマイグレ想定)
    client.run(
        "?[id, role, content, session_id, timestamp] := "
        "*episode{id, role, content, session_id, timestamp} "
        ":put episode {id => role, content, session_id, timestamp}"
    )
    fake = SmartFakeLLM(node_queue=['{"kind": "topic", "title": "rescue"}'])
    result = backfill(db, llm=fake, progress=False)
    assert result["nodes_added"] == 1
    res = client.run("?[topic_id] := *episode{id: 1, topic_id}")
    assert res["rows"][0][0].startswith("alive-"), \
        "legacy episode が新方式 topic に救済されていない"
