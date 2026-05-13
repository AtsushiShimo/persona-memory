"""Cozo embedded への接続ヘルパ.

設計:
- backend は SQLite (Cozo の sqlite backend = 単一ファイル, トランザクション,
  簡易 backup). RocksDB は依存重く ARM Mac で build 失敗事例あるため見送り.
- 1 ペルソナ = 1 .cozo.db ファイル (旧 .db と並ぶ).
- relation 定義は idempotent. 既に存在する relation は無視.
- HNSW index も idempotent (CozoScript の `::hnsw create ...` は重複 create
  でエラーになるため、 introspection で存在確認してから走らせる).
"""
from __future__ import annotations

from pathlib import Path

from pycozo.client import Client

EMBEDDING_DIM = 768
SCHEMA_VERSION = "cozo-1"


def connect(db_path: Path) -> Client:
    """Cozo client を返す. ファイルが無ければ新規作成. ロックは Cozo 任せ.

    pycozo は pandas が入っていないと stderr に Traceback を吐くので
    その間だけ stderr を黙らせる (機能は壊れない. dict 経路で動く).
    """
    import contextlib
    import io
    import sys
    db_path.parent.mkdir(parents=True, exist_ok=True)
    real_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        return Client("sqlite", str(db_path))
    finally:
        sys.stderr = real_stderr


# ── relation 定義 (idempotent) ─────────────────────────────────────────
# id は Int (旧 SQLite との互換性確保のため). 採番は id_seq relation で管理.
# fact / episode / discussion_node に embedding を直接持たせる
# (旧 SQLite の fact_embeddings 等の二重持ちを廃止).

RELATION_DEFS = [
    # facts
    """
    :create fact {
        id: Int =>
        category: String,
        key: String,
        value: String,
        importance: Int,
        access_count: Int default 0,
        status: String default 'active',
        supersedes: Int? default null,
        superseded_by: Int? default null,
        source: String? default null,
        reason_superseded: String? default null,
        created_at: String,
        updated_at: String,
        last_accessed_at: String? default null,
        embedding: <F32; 768>? default null,
    }
    """,
    # episodes
    """
    :create episode {
        id: Int =>
        role: String,
        content: String,
        summary: String? default null,
        session_id: String,
        topic_id: String? default null,
        timestamp: String,
        embedding: <F32; 768>? default null,
    }
    """,
    # discussion graph
    """
    :create discussion_node {
        id: Int =>
        ts: String,
        episode_id: Int? default null,
        kind: String,
        title: String,
        state: String default 'proposed',
        content: String? default null,
        embedding: <F32; 768>? default null,
    }
    """,
    # 「流れ」 を表す edge. キーは (from, to, kind) の組. 複数エッジが
    # 同じ向きで kind 違いで張れる (= 同意 + 派生 等).
    """
    :create discussion_edge {
        from_id: Int, to_id: Int, kind: String =>
        ts: String,
    }
    """,
    # topics
    """
    :create topic {
        id: String =>
        title: String? default null,
        summary: String? default null,
        created_at: String,
        last_active_at: String,
    }
    """,
    """
    :create topic_tag {
        id: Int =>
        topic_id: String,
        tag: String,
        ts: String,
        embedding: <F32; 768>? default null,
    }
    """,
    """
    :create topic_relation {
        from_topic_id: String, to_topic_id: String, kind: String =>
        ts: String,
    }
    """,
    # 雑多な KV. 旧 meta テーブル相当.
    """
    :create meta {
        key: String =>
        value: String,
    }
    """,
    # 採番用. entity 名 → 最後に発行した id.
    """
    :create id_seq {
        entity: String =>
        last_id: Int,
    }
    """,
    # 既存 conflicts / lint_log は維持 (今は別モジュールから書かれる)
    """
    :create conflict {
        id: Int =>
        fact_a_id: Int,
        fact_b_id: Int,
        confidence: Int,
        resolution: String,
        detected_at: String,
    }
    """,
    """
    :create lint_log {
        id: Int =>
        run_at: String,
        pairs_examined: Int default 0,
        conflicts_flagged: Int default 0,
        conflicts_auto_resolved: Int default 0,
        trigger_kind: String,
    }
    """,
    # 想起トリガー学習 (Phase 2 で必要なら復活. 当面は relation のみ.)
    """
    :create recall_trigger {
        id: Int =>
        ts: String,
        source_episode_id: Int? default null,
        trigger_phrase: String,
        query_embedding: <F32; 768>? default null,
        hit_fact_ids: String default '[]',
        hit_episode_ids: String default '[]',
    }
    """,
]

# HNSW index 定義 (relation, index 名, fields).
HNSW_INDEXES = [
    ("fact", "vec_idx", "[embedding]"),
    ("episode", "vec_idx", "[embedding]"),
    ("discussion_node", "vec_idx", "[embedding]"),
    ("topic_tag", "vec_idx", "[embedding]"),
    ("recall_trigger", "vec_idx", "[query_embedding]"),
]


def existing_relations(client: Client) -> set[str]:
    try:
        res = client.run("::relations")
    except Exception:
        return set()
    # 列名 'name' に relation 名
    out: set[str] = set()
    for row in res.get("rows", []):
        # rows は list of values; headers と並びが取れない場合は先頭が name
        if isinstance(row, list) and row:
            out.add(str(row[0]))
    return out


def existing_hnsw(client: Client, relation: str) -> set[str]:
    try:
        res = client.run(f"::indices {relation}")
    except Exception:
        return set()
    out: set[str] = set()
    for row in res.get("rows", []):
        if isinstance(row, list) and row:
            out.add(str(row[0]))
    return out


def init_db(db_path: Path) -> Client:
    """relation + HNSW index を idempotent に作成. client を返す."""
    client = connect(db_path)
    rels = existing_relations(client)
    for ddl in RELATION_DEFS:
        # `:create <name> {...}` の名前を取り出して存在チェック
        head = ddl.strip().split("{", 1)[0].strip()
        # 例: ":create fact"
        rel_name = head.split()[-1]
        if rel_name in rels:
            continue
        client.run(ddl)

    for relation, idx_name, fields in HNSW_INDEXES:
        idxs = existing_hnsw(client, relation)
        if idx_name in idxs:
            continue
        try:
            client.run(
                f"::hnsw create {relation}:{idx_name} "
                f"{{dim: {EMBEDDING_DIM}, m: 16, dtype: F32, "
                f"fields: {fields}, distance: Cosine, ef_construction: 50}}"
            )
        except Exception:
            # relation 自体がまだなら HNSW 作成は失敗するが、
            # 通常 RELATION_DEFS で先に作っているので発生しないはず.
            pass

    # schema_version を meta に記録 (idempotent, INSERT OR REPLACE 相当)
    client.run(
        "?[key, value] <- [['schema_version', $v]] :put meta {key => value}",
        {"v": SCHEMA_VERSION},
    )
    client.run(
        "?[key, value] <- [['embedding_dim', $v]] :put meta {key => value}",
        {"v": str(EMBEDDING_DIM)},
    )
    return client


def next_id(client: Client, entity: str) -> int:
    """採番. id_seq[entity] を atomically +1 して返す.

    並行性: pycozo はクライアント側で順次 run なので transaction 内に置けば
    十分. 大規模並行 writer を想定しないため簡易実装.
    """
    res = client.run(
        "?[last_id] := *id_seq{entity: $e, last_id} :limit 1",
        {"e": entity},
    )
    rows = res.get("rows", [])
    last = rows[0][0] if rows else 0
    new_id = last + 1
    client.run(
        "?[entity, last_id] <- [[$e, $v]] :put id_seq {entity => last_id}",
        {"e": entity, "v": new_id},
    )
    return new_id


def set_max_id(client: Client, entity: str, value: int) -> None:
    """migration で外部から id を流し込む時に id_seq を max(id) で正す."""
    client.run(
        "?[entity, last_id] <- [[$e, $v]] :put id_seq {entity => last_id}",
        {"e": entity, "v": value},
    )
