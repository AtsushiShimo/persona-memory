"""議論グラフの低レベル CRUD + 状態遷移 helper (0.6.13 Phase A1).

設計指針:
- ノードは削除しない (state 遷移のみ). 「忘れない」 原則.
- 検索ランキングを介さず "末端 accepted ノード即答" を実現する素材.
- LLM 統合 (write LLM が discussion node を抽出) は Phase A2 で別実装.

ノード kind:
  topic       — 論点 / 問い
  option      — 検討案 / 選択肢
  decision    — 採用判断 (state='accepted' で「決まった」 とみなす)
  retraction  — 明示的な撤回
  rationale   — 理由・前提

ノード state:
  proposed    — 提案中
  accepted    — 採用済 (= 結論)
  rejected    — 却下
  superseded  — 後続で上書きされた (履歴として残す)
  observed    — 観察事実 (topic 以外で proposed/accepted/rejected の枠に乗らないもの)

エッジ kind:
  considers     — topic → option (この論点はこの案を含む)
  decides       — option → decision (この案を採用)
  retracts      — retraction → 任意 (撤回)
  supersedes    — 新 → 旧 (上書き)
  derives_from  — 派生 (任意 → 任意)
  depends_on    — 前提 (任意 → 任意)
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

VALID_KINDS = {"topic", "option", "decision", "retraction", "rationale"}
VALID_STATES = {"proposed", "accepted", "rejected", "superseded", "observed"}
VALID_EDGE_KINDS = {
    "considers", "decides", "retracts", "supersedes", "derives_from", "depends_on",
}


def add_node(
    conn: sqlite3.Connection,
    kind: str,
    title: str,
    *,
    episode_id: int | None = None,
    state: str = "proposed",
    content: str | None = None,
    embedding: bytes | None = None,
) -> int:
    """新ノードを追加し、 そのノード id を返す."""
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind!r} (allowed: {VALID_KINDS})")
    if state not in VALID_STATES:
        raise ValueError(f"invalid state: {state!r} (allowed: {VALID_STATES})")
    cur = conn.execute(
        "INSERT INTO discussion_nodes (episode_id, kind, title, state, content, embedding) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (episode_id, kind, title, state, content, embedding),
    )
    conn.commit()
    return cur.lastrowid


def add_edge(
    conn: sqlite3.Connection, src_id: int, dst_id: int, edge_kind: str,
) -> int:
    """ノード間にエッジを追加."""
    if edge_kind not in VALID_EDGE_KINDS:
        raise ValueError(
            f"invalid edge_kind: {edge_kind!r} (allowed: {VALID_EDGE_KINDS})"
        )
    cur = conn.execute(
        "INSERT INTO discussion_edges (src_id, dst_id, edge_kind) VALUES (?, ?, ?)",
        (src_id, dst_id, edge_kind),
    )
    conn.commit()
    return cur.lastrowid


def transition_state(
    conn: sqlite3.Connection, node_id: int, new_state: str,
) -> None:
    """ノードの state を遷移. 履歴は別ノード (例: 旧 → superseded) で表現するため、
    本関数は単純 UPDATE. 「忘れない」 のは内容であって state そのものではない."""
    if new_state not in VALID_STATES:
        raise ValueError(f"invalid state: {new_state!r}")
    conn.execute(
        "UPDATE discussion_nodes SET state=? WHERE id=?",
        (new_state, node_id),
    )
    conn.commit()


def supersede_node(
    conn: sqlite3.Connection, old_id: int, new_id: int,
) -> int:
    """旧ノードを superseded に降格 + supersedes エッジで新ノードに連結.

    案 3 (反事実記憶) との接続点: 撤回理由ノード (kind='rationale') を
    別途作って retracts エッジで張れば、 撤回経緯まで構造化できる.

    戻り値: 追加された supersedes edge の id.
    """
    transition_state(conn, old_id, "superseded")
    return add_edge(conn, src_id=new_id, dst_id=old_id, edge_kind="supersedes")


def latest_decision_for_topic(
    conn: sqlite3.Connection, topic_id: int,
) -> dict | None:
    """指定 topic ノードに紐づく accepted decision を 1 件返す (最新 ts).

    「あの議論はどう決まった?」 への直答に使う. ランキング検索なしで答えを取り出す.
    """
    # topic → considers → option → decides → decision (accepted) のパスを辿る
    row = conn.execute(
        """
        SELECT d.id, d.title, d.content, d.ts, d.state
          FROM discussion_edges e1
          JOIN discussion_nodes opt ON opt.id = e1.dst_id
          JOIN discussion_edges e2 ON e2.src_id = opt.id
          JOIN discussion_nodes d   ON d.id = e2.dst_id
         WHERE e1.src_id = ?
           AND e1.edge_kind = 'considers'
           AND e2.edge_kind = 'decides'
           AND d.kind = 'decision'
           AND d.state = 'accepted'
         ORDER BY d.ts DESC
         LIMIT 1
        """,
        (topic_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "title": row[1], "content": row[2],
        "ts": row[3], "state": row[4],
    }


def neighbors(
    conn: sqlite3.Connection, node_id: int, edge_kinds: Iterable[str] | None = None,
) -> list[dict]:
    """1-hop 近傍を返す (双方向). edge_kinds で絞り込み可."""
    if edge_kinds:
        kinds = list(edge_kinds)
        placeholders = ",".join("?" * len(kinds))
        rows = conn.execute(
            f"""
            SELECT n.id, n.kind, n.title, n.state, e.edge_kind, e.src_id, e.dst_id
              FROM discussion_edges e
              JOIN discussion_nodes n ON n.id = CASE
                  WHEN e.src_id = ? THEN e.dst_id ELSE e.src_id END
             WHERE (e.src_id = ? OR e.dst_id = ?)
               AND e.edge_kind IN ({placeholders})
            """,
            (node_id, node_id, node_id, *kinds),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT n.id, n.kind, n.title, n.state, e.edge_kind, e.src_id, e.dst_id
              FROM discussion_edges e
              JOIN discussion_nodes n ON n.id = CASE
                  WHEN e.src_id = ? THEN e.dst_id ELSE e.src_id END
             WHERE e.src_id = ? OR e.dst_id = ?
            """,
            (node_id, node_id, node_id),
        ).fetchall()
    return [
        {
            "id": r[0], "kind": r[1], "title": r[2], "state": r[3],
            "edge_kind": r[4],
            "direction": "out" if r[5] == node_id else "in",
        }
        for r in rows
    ]
