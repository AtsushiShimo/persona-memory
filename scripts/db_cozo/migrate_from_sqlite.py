"""SQLite (旧 schema) → Cozo (新 schema) への migration.

設計:
- 旧 .db を物理コピーで .db.bak.<ts> として残す (= マスター承認の安全策).
- relation 単位で順次転送. embedding は vec0 の BLOB を unpack して
  list[float] に戻し、 Cozo の vec() に流し込む.
- :put は key で upsert なので re-run しても整合.
- migration 完了後、 各 relation の max(id) を id_seq にセット.
- 失敗時は Cozo 側を削除して再実行できるよう、 中間状態でも壊れない.

実行例:
  python -m scripts.db_cozo.migrate_from_sqlite \\
      --src ~/.persona-memory/<persona>.db \\
      --dst ~/.persona-memory/<persona>.cozo.db
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import struct
import sys
import time
from pathlib import Path

import sqlite_vec

from scripts.db_cozo.connection import EMBEDDING_DIM, init_db, set_max_id


def open_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def backup_sqlite(src: Path) -> Path:
    """旧 DB を .db.bak.<unix_ts> にコピー. パスを返す."""
    bak = src.with_suffix(src.suffix + f".bak.{int(time.time())}")
    shutil.copy2(src, bak)
    return bak


def _unpack(blob: bytes | None) -> list[float] | None:
    if blob is None:
        return None
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','virtual table') "
        "AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def migrate_facts(src: sqlite3.Connection, dst, progress: bool = True) -> int:
    """旧 facts → 新 fact. fact_embeddings から embedding を join."""
    rows = src.execute("""
        SELECT
          f.id, f.category, f.key, f.value, f.importance,
          COALESCE(f.access_count, 0) AS access_count,
          f.status,
          f.supersedes, f.superseded_by, f.source, f.reason_superseded,
          f.created_at, f.updated_at, f.last_accessed_at,
          fe.embedding
        FROM facts f LEFT JOIN fact_embeddings fe ON fe.fact_id = f.id
    """).fetchall()
    n = 0
    for r in rows:
        emb = _unpack(r["embedding"])
        params: dict = {
            "id": r["id"], "cat": r["category"], "key": r["key"],
            "val": r["value"], "imp": r["importance"],
            "ac": r["access_count"], "st": r["status"],
            "sup": r["supersedes"], "sb": r["superseded_by"],
            "src": r["source"], "rs": r["reason_superseded"],
            "ca": r["created_at"], "ua": r["updated_at"],
            "la": r["last_accessed_at"],
        }
        if emb is not None and len(emb) == EMBEDDING_DIM:
            dst.run(
                "?[id, category, key, value, importance, access_count, status, "
                "supersedes, superseded_by, source, reason_superseded, "
                "created_at, updated_at, last_accessed_at, embedding] <- "
                "[[$id, $cat, $key, $val, $imp, $ac, $st, $sup, $sb, $src, $rs, "
                "$ca, $ua, $la, vec($emb)]] "
                ":put fact {id => category, key, value, importance, access_count, "
                "status, supersedes, superseded_by, source, reason_superseded, "
                "created_at, updated_at, last_accessed_at, embedding}",
                {**params, "emb": emb},
            )
        else:
            dst.run(
                "?[id, category, key, value, importance, access_count, status, "
                "supersedes, superseded_by, source, reason_superseded, "
                "created_at, updated_at, last_accessed_at] <- "
                "[[$id, $cat, $key, $val, $imp, $ac, $st, $sup, $sb, $src, $rs, "
                "$ca, $ua, $la]] "
                ":put fact {id => category, key, value, importance, access_count, "
                "status, supersedes, superseded_by, source, reason_superseded, "
                "created_at, updated_at, last_accessed_at}",
                params,
            )
        n += 1
        if progress and n % 100 == 0:
            sys.stderr.write(f"  fact: {n}\n")
    if rows:
        set_max_id(dst, "fact", max(r["id"] for r in rows))
    return n


def migrate_episodes(src: sqlite3.Connection, dst, progress: bool = True) -> int:
    rows = src.execute("""
        SELECT
          e.id, e.role, e.content, e.summary, e.session_id,
          e.timestamp,
          ee.embedding,
          CASE WHEN EXISTS (SELECT 1 FROM pragma_table_info('episodes') WHERE name='topic_id')
               THEN e.topic_id ELSE NULL END AS topic_id_maybe
        FROM episodes e LEFT JOIN episode_embeddings ee ON ee.episode_id = e.id
    """).fetchall()
    n = 0
    for r in rows:
        emb = _unpack(r["embedding"])
        topic_id = r["topic_id_maybe"]
        params: dict = {
            "id": r["id"], "role": r["role"], "content": r["content"],
            "sum": r["summary"], "sid": r["session_id"],
            "tid": topic_id, "ts": r["timestamp"],
        }
        if emb is not None and len(emb) == EMBEDDING_DIM:
            dst.run(
                "?[id, role, content, summary, session_id, topic_id, timestamp, embedding] "
                "<- [[$id, $role, $content, $sum, $sid, $tid, $ts, vec($emb)]] "
                ":put episode {id => role, content, summary, session_id, topic_id, "
                "timestamp, embedding}",
                {**params, "emb": emb},
            )
        else:
            dst.run(
                "?[id, role, content, summary, session_id, topic_id, timestamp] "
                "<- [[$id, $role, $content, $sum, $sid, $tid, $ts]] "
                ":put episode {id => role, content, summary, session_id, topic_id, "
                "timestamp}",
                params,
            )
        n += 1
        if progress and n % 200 == 0:
            sys.stderr.write(f"  episode: {n}\n")
    if rows:
        set_max_id(dst, "episode", max(r["id"] for r in rows))
    return n


def migrate_discussion_nodes(src: sqlite3.Connection, dst) -> int:
    if not _table_exists(src, "discussion_nodes"):
        return 0
    rows = src.execute(
        "SELECT id, ts, episode_id, kind, title, state, content, embedding "
        "FROM discussion_nodes"
    ).fetchall()
    n = 0
    for r in rows:
        emb = _unpack(r["embedding"])
        params = {
            "id": r["id"], "ts": r["ts"], "eid": r["episode_id"],
            "kind": r["kind"], "title": r["title"],
            "state": r["state"], "content": r["content"],
        }
        if emb and len(emb) == EMBEDDING_DIM:
            dst.run(
                "?[id, ts, episode_id, kind, title, state, content, embedding] "
                "<- [[$id, $ts, $eid, $kind, $title, $state, $content, vec($emb)]] "
                ":put discussion_node {id => ts, episode_id, kind, title, state, "
                "content, embedding}",
                {**params, "emb": emb},
            )
        else:
            dst.run(
                "?[id, ts, episode_id, kind, title, state, content] "
                "<- [[$id, $ts, $eid, $kind, $title, $state, $content]] "
                ":put discussion_node {id => ts, episode_id, kind, title, state, content}",
                params,
            )
        n += 1
    if rows:
        set_max_id(dst, "discussion_node", max(r["id"] for r in rows))
    return n


def migrate_discussion_edges(src: sqlite3.Connection, dst) -> int:
    if not _table_exists(src, "discussion_edges"):
        return 0
    rows = src.execute(
        "SELECT src_id, dst_id, edge_kind, ts FROM discussion_edges"
    ).fetchall()
    n = 0
    for r in rows:
        dst.run(
            "?[from_id, to_id, kind, ts] <- [[$f, $t, $k, $ts]] "
            ":put discussion_edge {from_id, to_id, kind => ts}",
            {"f": r["src_id"], "t": r["dst_id"], "k": r["edge_kind"], "ts": r["ts"]},
        )
        n += 1
    return n


def migrate_topics(src: sqlite3.Connection, dst) -> int:
    if not _table_exists(src, "topics"):
        return 0
    rows = src.execute(
        "SELECT id, title, summary, created_at, last_active_at FROM topics"
    ).fetchall()
    n = 0
    for r in rows:
        dst.run(
            "?[id, title, summary, created_at, last_active_at] "
            "<- [[$id, $title, $summary, $ca, $la]] "
            ":put topic {id => title, summary, created_at, last_active_at}",
            {"id": r["id"], "title": r["title"], "summary": r["summary"],
             "ca": r["created_at"], "la": r["last_active_at"]},
        )
        n += 1
    return n


def migrate_topic_tags(src: sqlite3.Connection, dst) -> int:
    if not _table_exists(src, "topic_tags"):
        return 0
    has_emb = _table_exists(src, "topic_tag_embeddings")
    if has_emb:
        sql = ("SELECT t.id, t.topic_id, t.tag, t.ts, te.embedding "
               "FROM topic_tags t LEFT JOIN topic_tag_embeddings te "
               "ON te.topic_tag_id = t.id")
    else:
        sql = "SELECT id, topic_id, tag, ts, NULL AS embedding FROM topic_tags"
    rows = src.execute(sql).fetchall()
    n = 0
    for r in rows:
        emb = _unpack(r["embedding"])
        params = {"id": r["id"], "tid": r["topic_id"], "tag": r["tag"], "ts": r["ts"]}
        if emb and len(emb) == EMBEDDING_DIM:
            dst.run(
                "?[id, topic_id, tag, ts, embedding] <- "
                "[[$id, $tid, $tag, $ts, vec($emb)]] "
                ":put topic_tag {id => topic_id, tag, ts, embedding}",
                {**params, "emb": emb},
            )
        else:
            dst.run(
                "?[id, topic_id, tag, ts] <- [[$id, $tid, $tag, $ts]] "
                ":put topic_tag {id => topic_id, tag, ts}",
                params,
            )
        n += 1
    if rows:
        set_max_id(dst, "topic_tag", max(r["id"] for r in rows))
    return n


def migrate_topic_relations(src: sqlite3.Connection, dst) -> int:
    if not _table_exists(src, "topic_relations"):
        return 0
    rows = src.execute(
        "SELECT from_topic_id, to_topic_id, kind, ts FROM topic_relations"
    ).fetchall()
    n = 0
    for r in rows:
        dst.run(
            "?[from_topic_id, to_topic_id, kind, ts] <- [[$f, $t, $k, $ts]] "
            ":put topic_relation {from_topic_id, to_topic_id, kind => ts}",
            {"f": r["from_topic_id"], "t": r["to_topic_id"], "k": r["kind"], "ts": r["ts"]},
        )
        n += 1
    return n


def migrate_meta(src: sqlite3.Connection, dst) -> int:
    rows = src.execute("SELECT key, value FROM meta").fetchall()
    n = 0
    for r in rows:
        # schema_version は新側を上書きしない (init_db が cozo-1 をセット)
        if r["key"] == "schema_version":
            continue
        dst.run(
            "?[key, value] <- [[$k, $v]] :put meta {key => value}",
            {"k": r["key"], "v": r["value"]},
        )
        n += 1
    return n


def migrate_recall_triggers(src: sqlite3.Connection, dst) -> int:
    if not _table_exists(src, "recall_triggers"):
        return 0
    rows = src.execute(
        "SELECT id, ts, source_episode_id, trigger_phrase, query_embedding, "
        "hit_fact_ids, hit_episode_ids FROM recall_triggers"
    ).fetchall()
    n = 0
    for r in rows:
        emb = _unpack(r["query_embedding"])
        params = {
            "id": r["id"], "ts": r["ts"], "se": r["source_episode_id"],
            "tp": r["trigger_phrase"], "hf": r["hit_fact_ids"], "he": r["hit_episode_ids"],
        }
        if emb and len(emb) == EMBEDDING_DIM:
            dst.run(
                "?[id, ts, source_episode_id, trigger_phrase, query_embedding, "
                "hit_fact_ids, hit_episode_ids] <- "
                "[[$id, $ts, $se, $tp, vec($emb), $hf, $he]] "
                ":put recall_trigger {id => ts, source_episode_id, trigger_phrase, "
                "query_embedding, hit_fact_ids, hit_episode_ids}",
                {**params, "emb": emb},
            )
        else:
            dst.run(
                "?[id, ts, source_episode_id, trigger_phrase, hit_fact_ids, hit_episode_ids] "
                "<- [[$id, $ts, $se, $tp, $hf, $he]] "
                ":put recall_trigger {id => ts, source_episode_id, trigger_phrase, "
                "hit_fact_ids, hit_episode_ids}",
                params,
            )
        n += 1
    if rows:
        set_max_id(dst, "recall_trigger", max(r["id"] for r in rows))
    return n


def migrate_conflicts_and_lint(src: sqlite3.Connection, dst) -> tuple[int, int]:
    nc = 0
    if _table_exists(src, "conflicts"):
        rows = [
            [r["id"], r["fact_a_id"], r["fact_b_id"], r["confidence"],
             r["resolution"], r["detected_at"]]
            for r in src.execute(
                "SELECT id, fact_a_id, fact_b_id, confidence, resolution, detected_at "
                "FROM conflicts"
            ).fetchall()
        ]
        if rows:
            dst.run(
                "?[id, fact_a_id, fact_b_id, confidence, resolution, detected_at] "
                "<- $rows "
                ":put conflict {id => fact_a_id, fact_b_id, confidence, "
                "resolution, detected_at}",
                {"rows": rows},
            )
            nc = len(rows)
            set_max_id(dst, "conflict", max(r[0] for r in rows))
    nl = 0
    if _table_exists(src, "lint_log"):
        rows = [
            [r["id"], r["run_at"], r["pairs_examined"], r["conflicts_flagged"],
             r["conflicts_auto_resolved"], r["trigger_kind"]]
            for r in src.execute(
                "SELECT id, run_at, pairs_examined, conflicts_flagged, "
                "conflicts_auto_resolved, trigger_kind FROM lint_log"
            ).fetchall()
        ]
        if rows:
            dst.run(
                "?[id, run_at, pairs_examined, conflicts_flagged, "
                "conflicts_auto_resolved, trigger_kind] <- $rows "
                ":put lint_log {id => run_at, pairs_examined, conflicts_flagged, "
                "conflicts_auto_resolved, trigger_kind}",
                {"rows": rows},
            )
            nl = len(rows)
            set_max_id(dst, "lint_log", max(r[0] for r in rows))
    return nc, nl


def migrate(src_path: Path, dst_path: Path, progress: bool = True) -> dict:
    bak = backup_sqlite(src_path)
    if progress:
        sys.stderr.write(f"backup: {bak}\n")
    src = open_sqlite(src_path)
    dst = init_db(dst_path)
    try:
        n_meta = migrate_meta(src, dst)
        n_facts = migrate_facts(src, dst, progress=progress)
        n_eps = migrate_episodes(src, dst, progress=progress)
        n_dn = migrate_discussion_nodes(src, dst)
        n_de = migrate_discussion_edges(src, dst)
        n_topics = migrate_topics(src, dst)
        n_tags = migrate_topic_tags(src, dst)
        n_trel = migrate_topic_relations(src, dst)
        n_rt = migrate_recall_triggers(src, dst)
        n_conf, n_lint = migrate_conflicts_and_lint(src, dst)
    finally:
        src.close()
    return {
        "backup": str(bak),
        "facts": n_facts, "episodes": n_eps,
        "discussion_nodes": n_dn, "discussion_edges": n_de,
        "topics": n_topics, "topic_tags": n_tags, "topic_relations": n_trel,
        "recall_triggers": n_rt, "conflicts": n_conf, "lint_logs": n_lint,
        "meta": n_meta,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Migrate SQLite persona DB to Cozo")
    p.add_argument("--src", required=True, type=Path, help="旧 SQLite .db")
    p.add_argument("--dst", required=True, type=Path, help="新 .cozo.db")
    p.add_argument("--no-progress", action="store_true")
    args = p.parse_args(argv)

    if not args.src.exists():
        sys.stderr.write(f"src not found: {args.src}\n")
        return 1
    result = migrate(args.src, args.dst, progress=not args.no_progress)
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
