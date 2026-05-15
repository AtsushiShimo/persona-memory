"""議論グラフ (discussion_node + discussion_edge) を 3D 可視化用 JSON に dump.

設計:
- on-demand 実行のみ (hook 経路では呼ばない)
- Cozo から discussion_node + discussion_edge + topic を引き、 self-contained HTML
  ファイルを生成. ブラウザで開けば 3D force-directed graph として閲覧可能.

可視化マッピング (HTML テンプレ側で対応):
- node 色: kind (topic / option / decision / retraction / rationale / observation /
  question / answer)
- node 透明度: state (proposed / accepted / rejected / superseded / observed)
- edge 色 + 矢印: kind (賛同 / 反論 / 派生 / 決定 / 次バトン / 観察 / 結論 /
  質問 / 回答 / 撤回)
- 同 topic_id の node は force グループ化

実行例:
  python -m scripts.db_cozo.visualize --db <persona>.cozo.db
  python -m scripts.db_cozo.visualize --db ... --topic t-renju
  python -m scripts.db_cozo.visualize --db ... --limit 200
  python -m scripts.db_cozo.visualize --db ... --days 7
  python -m scripts.db_cozo.visualize --db ... --json-only > graph.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import string
import sys
from pathlib import Path

from pycozo.client import Client

from scripts.db_cozo.connection import init_db


def _fetch_nodes(
    client: Client,
    topic_id: str | None = None,
    since_ts: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """discussion_node を取得し、 episode 経由で topic_id を結合.

    返り値: [{id, kind, title, state, content, episode_id, topic_id, ts}, ...]
    """
    res_nodes = client.run(
        "?[id, kind, title, state, content, episode_id, ts] := "
        "*discussion_node{id, kind, title, state, content, episode_id, ts} "
        ":order id",
    )
    nodes_raw = res_nodes.get("rows", [])
    ep_ids = [r[5] for r in nodes_raw if r[5] is not None]
    ep_to_topic: dict[int, str | None] = {}
    if ep_ids:
        res_ep = client.run(
            "?[id, topic_id] := *episode{id, topic_id}, id in $ids",
            {"ids": ep_ids},
        )
        ep_to_topic = {r[0]: r[1] for r in res_ep.get("rows", [])}
    rows = []
    for r in nodes_raw:
        eid = r[5]
        tid_val = ep_to_topic.get(eid) if eid is not None else None
        row = {
            "id": r[0], "kind": r[1], "title": r[2], "state": r[3],
            "content": r[4], "episode_id": eid, "topic_id": tid_val,
            "ts": r[6],
        }
        if topic_id and row["topic_id"] != topic_id:
            continue
        if since_ts and (row["ts"] or "") < since_ts:
            continue
        rows.append(row)
    if limit is not None:
        rows = rows[-limit:]  # 最新側を残す
    return rows


def _fetch_edges(client: Client, node_ids: set[int]) -> list[dict]:
    """対象 node 間の edge のみ. 両端が node_ids に含まれる edge を抽出."""
    if not node_ids:
        return []
    res = client.run(
        "?[from_id, to_id, kind, ts] := *discussion_edge{from_id, to_id, kind, ts}",
    )
    edges = []
    for r in res.get("rows", []):
        if r[0] in node_ids and r[1] in node_ids:
            edges.append({
                "source": r[0], "target": r[1],
                "kind": r[2], "ts": r[3],
            })
    return edges


def _fetch_topics(client: Client, topic_ids: set[str]) -> list[dict]:
    """node 集合に出現する topic_id の詳細を取得."""
    valid_ids = [t for t in topic_ids if t]
    if not valid_ids:
        return []
    res = client.run(
        "?[id, title, summary, last_active_at] := "
        "*topic{id, title, summary, last_active_at}, id in $ids",
        {"ids": valid_ids},
    )
    return [
        {"id": r[0], "title": r[1], "summary": r[2], "last_active_at": r[3]}
        for r in res.get("rows", [])
    ]


def build_graph(
    db_path: Path,
    topic_id: str | None = None,
    days: int | None = None,
    limit: int | None = None,
) -> dict:
    """Cozo DB から graph JSON 構造を組み立てる."""
    client = init_db(db_path)
    since_ts = None
    if days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        since_ts = cutoff.isoformat()
    nodes = _fetch_nodes(
        client, topic_id=topic_id, since_ts=since_ts, limit=limit,
    )
    node_ids = {n["id"] for n in nodes}
    edges = _fetch_edges(client, node_ids)
    topic_ids = {n["topic_id"] for n in nodes}
    topics = _fetch_topics(client, topic_ids)
    return {
        "meta": {
            "db_path": str(db_path),
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "topic_count": len(topics),
            "filter": {
                "topic": topic_id, "days": days, "limit": limit,
            },
        },
        "topics": topics,
        "nodes": nodes,
        "links": edges,
    }


def _template_path() -> Path:
    return Path(__file__).parent.parent.parent / "templates" / "graph_viewer.html"


def render_html(graph: dict, template: str | None = None) -> str:
    """self-contained HTML を生成. data を JSON として埋め込み."""
    if template is None:
        tpl_path = _template_path()
        template = tpl_path.read_text(encoding="utf-8")
    payload = json.dumps(graph, ensure_ascii=False)
    # string.Template だと JS の $ と衝突するので単純 replace
    return template.replace("__GRAPH_DATA_JSON__", payload)


def write_html(
    graph: dict, out_dir: Path,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"graph-{ts}.html"
    out_path.write_text(render_html(graph), encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="議論グラフを 3D 可視化用 HTML に dump",
    )
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--topic", default=None, help="topic_id で絞り込み")
    p.add_argument("--days", type=int, default=None, help="過去 N 日のみ")
    p.add_argument("--limit", type=int, default=None,
                   help="最新 N node に絞る (混雑回避)")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="HTML 出力先 (default: <db dir>/visualize/)")
    p.add_argument("--json-only", action="store_true",
                   help="JSON を stdout に出して終了 (HTML 生成しない)")
    args = p.parse_args(argv)
    if not args.db.exists():
        sys.stderr.write(f"db not found: {args.db}\n")
        return 1
    graph = build_graph(
        args.db, topic_id=args.topic, days=args.days, limit=args.limit,
    )
    if args.json_only:
        print(json.dumps(graph, ensure_ascii=False, indent=2))
        return 0
    out_dir = args.out_dir or (args.db.parent / "visualize")
    out_path = write_html(graph, out_dir)
    print(f"  generated: {out_path}")
    print(
        f"  nodes={graph['meta']['node_count']} "
        f"edges={graph['meta']['edge_count']} "
        f"topics={graph['meta']['topic_count']}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
