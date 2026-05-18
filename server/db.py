"""MCP server backend (Cozo).

0.8.0 から SQLite を業務フローから完全に外し、 Cozo 単独経路に統一した.
旧 SQLite 経路は読み専用バックアップとして物理残置されるが、 本 MCP server
からは参照しない. 関数シグネチャは旧 SQLite 版と互換を保ち、 上位 server/main.py
は変更不要.

db_path() は引き続き <persona>.db (SQLite path) を返す — これは
.persona-memory ディレクトリの persona 名解決の手がかりとして機能する.
実際の読み書きは <persona>.cozo.db に対して行う.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path

DB_PATH_ENV = "PERSONA_MEMORY_DB"

_PERSONA_NAME_BRACKET_RE = re.compile(r"[『「](.+?)[』」]")


def db_path() -> Path:
    """SQLite path (= persona 名解決の手がかり). 実体は使わない."""
    raw = os.environ.get(DB_PATH_ENV)
    if not raw:
        raise RuntimeError(
            f"Set {DB_PATH_ENV} to the persona memory DB path "
            f"(e.g. /path/to/persona.db)."
        )
    p = Path(raw).expanduser()
    if not p.exists():
        raise RuntimeError(
            f"DB not found: {p}. Run /persona-memory:init first."
        )
    return p


def _cozo_path() -> Path:
    """`<persona>.db` → `<persona>.cozo.db`. 既に `.cozo.db` ならそのまま."""
    from scripts.db_cozo.wire import cozo_db_path_for
    return cozo_db_path_for(db_path())


def _cozo_client():
    """Cozo client を返す (毎呼出で new client. MCP server は stateless)."""
    from scripts.db_cozo.connection import init_db
    return init_db(_cozo_path())


# ── persona 名解決 ────────────────────────────────────────────────────

def get_persona_name(default: str = "AI アシスタント") -> str:
    """`persona/identity` fact の値から名前 (『〈name〉』) を抽出."""
    try:
        client = _cozo_client()
        res = client.run(
            "?[value] := *fact{category, key, value, status: 'active'}, "
            "category = 'persona', key = 'identity'",
        )
        rows = res.get("rows", [])
        if not rows:
            return default
        m = _PERSONA_NAME_BRACKET_RE.search(rows[0][0] or "")
        if m:
            return m.group(1).strip() or default
        return default
    except Exception:
        return default


# ── fact 系 ──────────────────────────────────────────────────────────

def upsert_fact(
    *,
    category: str,
    key: str,
    value: str,
    importance: int = 5,
    source: str | None = None,
) -> int:
    """fact を upsert. 既存 active があれば内容更新, 無ければ新規 insert.

    Cozo には CHECK 制約が無いため category 'lesson' / importance 10 も受理.
    """
    from scripts.db_cozo.connection import next_id
    import datetime as dt
    tz = dt.timezone(dt.timedelta(hours=9))
    ts = dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    client = _cozo_client()
    # 既存 active 検索
    res = client.run(
        "?[id] := *fact{id, category, key, status: 'active'}, "
        "category = $c, key = $k :limit 1",
        {"c": category, "k": key},
    )
    rows = res.get("rows", [])
    if rows:
        fid = rows[0][0]
        # 値更新 (= 既存 fact をそのまま :put で書き換え)
        client.run(
            "?[id, category, key, value, importance, status, source, "
            "created_at, updated_at, access_count] <- "
            "[[$id, $c, $k, $v, $imp, 'active', $src, $ts, $ts, 0]] "
            ":put fact {id => category, key, value, importance, status, "
            "source, created_at, updated_at, access_count}",
            {"id": fid, "c": category, "k": key, "v": value,
             "imp": importance, "src": source, "ts": ts},
        )
        return fid
    fid = next_id(client, "fact")
    client.run(
        "?[id, category, key, value, importance, status, source, "
        "created_at, updated_at, access_count] <- "
        "[[$id, $c, $k, $v, $imp, 'active', $src, $ts, $ts, 0]] "
        ":put fact {id => category, key, value, importance, status, "
        "source, created_at, updated_at, access_count}",
        {"id": fid, "c": category, "k": key, "v": value,
         "imp": importance, "src": source, "ts": ts},
    )
    return fid


def write_fact_embedding(fact_id: int, embedding_blob_or_list) -> None:
    """fact relation の embedding 列を更新.

    互換のため引数名は旧版 (`embedding_blob: bytes`) を踏襲しているが、
    Cozo は <F32; 768> 型なので list[float] を受け付ける. bytes が来たら
    768 個の f32 として unpack して渡す.
    """
    import struct
    if isinstance(embedding_blob_or_list, (bytes, bytearray, memoryview)):
        vec = list(struct.unpack(f"{768}f", bytes(embedding_blob_or_list)))
    else:
        vec = list(embedding_blob_or_list)
    if not vec:
        return
    client = _cozo_client()
    # 既存 fact の embedding 列だけ更新する形 (= 他列は維持).
    # Cozo の :put は全列指定が必要なので一度読んでから書き直す.
    row = client.run(
        "?[category, key, value, importance, status, source, "
        "created_at, updated_at, access_count, supersedes, superseded_by, "
        "reason_superseded, last_accessed_at] := "
        "*fact{id: $id, category, key, value, importance, status, source, "
        "created_at, updated_at, access_count, supersedes, superseded_by, "
        "reason_superseded, last_accessed_at} :limit 1",
        {"id": fact_id},
    ).get("rows", [])
    if not row:
        return
    r = row[0]
    client.run(
        "?[id, category, key, value, importance, access_count, status, "
        "supersedes, superseded_by, source, reason_superseded, "
        "created_at, updated_at, last_accessed_at, embedding] <- "
        "[[$id, $c, $k, $v, $imp, $ac, $st, $sup, $supby, $src, $rs, "
        "$cr, $up, $la, $emb]] "
        ":put fact {id => category, key, value, importance, access_count, "
        "status, supersedes, superseded_by, source, reason_superseded, "
        "created_at, updated_at, last_accessed_at, embedding}",
        {"id": fact_id, "c": r[0], "k": r[1], "v": r[2], "imp": r[3],
         "ac": r[8], "st": r[4], "sup": r[9], "supby": r[10],
         "src": r[5], "rs": r[11], "cr": r[6], "up": r[7], "la": r[12],
         "emb": vec},
    )


def search_facts(
    *,
    embedding_blob: bytes,
    top_k: int,
    category: str | None,
) -> list[dict]:
    """fact をベクター検索. 互換のため `embedding_blob` は bytes 受けるが
    Cozo には list[float] を渡す."""
    import struct
    vec = list(struct.unpack(f"{768}f", bytes(embedding_blob)))
    client = _cozo_client()
    if category:
        res = client.run(
            "?[dist, id, c, k, v, imp, st] := "
            "~fact:vec_idx{id, category: c, key: k, value: v, importance: imp, "
            "status: st | query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
            "*fact{id, status: st}, "
            "st != 'superseded', c = $cat "
            ":order dist",
            {"q": vec, "k": top_k, "cat": category},
        )
    else:
        res = client.run(
            "?[dist, id, c, k, v, imp, st] := "
            "~fact:vec_idx{id, category: c, key: k, value: v, importance: imp, "
            "status: st | query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
            "*fact{id, status: st}, "
            "st != 'superseded' "
            ":order dist",
            {"q": vec, "k": top_k},
        )
    rows = res.get("rows", [])
    return [
        {
            "id": r[1], "category": r[2], "key": r[3], "value": r[4],
            "importance": r[5], "status": r[6], "distance": r[0],
        }
        for r in rows
    ]


def list_active_facts(limit: int = 1000) -> list[dict]:
    client = _cozo_client()
    res = client.run(
        "?[id, category, key, value] := "
        "*fact{id, category, key, value, status: 'active'} :order id",
    )
    rows = res.get("rows", [])[:limit]
    return [
        {"id": r[0], "category": r[1], "key": r[2], "value": r[3]}
        for r in rows
    ]


def get_fact(fact_id: int) -> dict | None:
    client = _cozo_client()
    res = client.run(
        "?[id, category, key, value, importance, status, source, "
        "created_at, updated_at] := "
        "*fact{id, category, key, value, importance, status, source, "
        "created_at, updated_at}, id = $id :limit 1",
        {"id": fact_id},
    )
    rows = res.get("rows", [])
    if not rows:
        return None
    r = rows[0]
    return {
        "id": r[0], "category": r[1], "key": r[2], "value": r[3],
        "importance": r[4], "status": r[5], "source": r[6],
        "created_at": r[7], "updated_at": r[8],
    }


def find_neighbors(fact_id: int, embedding_blob: bytes, top_k: int) -> list[dict]:
    """fact_id 以外の active 近傍."""
    import struct
    vec = list(struct.unpack(f"{768}f", bytes(embedding_blob)))
    client = _cozo_client()
    res = client.run(
        "?[dist, id, c, k, v] := "
        "~fact:vec_idx{id, category: c, key: k, value: v | "
        "query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
        "*fact{id, status: 'active'}, "
        "id != $fid "
        ":order dist",
        {"q": vec, "k": top_k + 1, "fid": fact_id},
    )
    rows = res.get("rows", [])
    return [
        {
            "fact_id": r[1], "distance": r[0],
            "category": r[2], "key": r[3], "value": r[4],
        }
        for r in rows[:top_k]
    ]


def flag_conflict(
    *,
    fact_a_id: int,
    fact_b_id: int,
    confidence: int,
    auto_resolved: bool,
) -> None:
    import datetime as dt
    from scripts.db_cozo.connection import next_id
    tz = dt.timezone(dt.timedelta(hours=9))
    ts = dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    client = _cozo_client()
    cid = next_id(client, "conflict")
    resolution = "auto_superseded" if auto_resolved else "pending"
    client.run(
        "?[id, fact_a_id, fact_b_id, confidence, resolution, detected_at] <- "
        "[[$id, $a, $b, $conf, $res, $ts]] "
        ":put conflict {id => fact_a_id, fact_b_id, confidence, "
        "resolution, detected_at}",
        {"id": cid, "a": fact_a_id, "b": fact_b_id,
         "conf": confidence, "res": resolution, "ts": ts},
    )
    # fact status 更新
    targets = [fact_b_id] if auto_resolved else [fact_a_id, fact_b_id]
    new_status = "superseded" if auto_resolved else "conflict"
    for fid in targets:
        # 既存 fact を read → status だけ書き換えて :put
        row = client.run(
            "?[c, k, v, imp, ac, st, sup, supby, src, rs, cr, up, la, emb] := "
            "*fact{id: $id, category: c, key: k, value: v, importance: imp, "
            "access_count: ac, status: st, supersedes: sup, superseded_by: supby, "
            "source: src, reason_superseded: rs, created_at: cr, updated_at: up, "
            "last_accessed_at: la, embedding: emb} :limit 1",
            {"id": fid},
        ).get("rows", [])
        if not row:
            continue
        r = row[0]
        client.run(
            "?[id, category, key, value, importance, access_count, status, "
            "supersedes, superseded_by, source, reason_superseded, "
            "created_at, updated_at, last_accessed_at, embedding] <- "
            "[[$id, $c, $k, $v, $imp, $ac, $st, $sup, $supby, $src, $rs, "
            "$cr, $up, $ts, $emb]] "
            ":put fact {id => category, key, value, importance, access_count, "
            "status, supersedes, superseded_by, source, reason_superseded, "
            "created_at, updated_at, last_accessed_at, embedding}",
            {"id": fid, "c": r[0], "k": r[1], "v": r[2], "imp": r[3],
             "ac": r[4], "st": new_status, "sup": r[6], "supby": r[7],
             "src": r[8], "rs": r[9], "cr": r[10], "up": ts, "la": r[12],
             "emb": r[13]},
        )


def record_lint_run(
    *, pairs_examined: int, conflicts_flagged: int, conflicts_auto_resolved: int
) -> None:
    import datetime as dt
    from scripts.db_cozo.connection import next_id
    tz = dt.timezone(dt.timedelta(hours=9))
    ts = dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    client = _cozo_client()
    lid = next_id(client, "lint_log")
    client.run(
        "?[id, run_at, pairs_examined, conflicts_flagged, "
        "conflicts_auto_resolved, trigger_kind] <- "
        "[[$id, $ts, $pe, $cf, $car, 'manual']] "
        ":put lint_log {id => run_at, pairs_examined, conflicts_flagged, "
        "conflicts_auto_resolved, trigger_kind}",
        {"id": lid, "ts": ts, "pe": pairs_examined,
         "cf": conflicts_flagged, "car": conflicts_auto_resolved},
    )


def forget_fact(*, category: str, key: str) -> dict | None:
    """Soft-delete (status='superseded')."""
    client = _cozo_client()
    row = client.run(
        "?[id, value] := *fact{id, category, key, value, status: 'active'}, "
        "category = $c, key = $k :limit 1",
        {"c": category, "k": key},
    ).get("rows", [])
    if not row:
        return None
    fid, value = row[0][0], row[0][1]
    flag_conflict(  # status='superseded' に書き換える経路を使い回す
        fact_a_id=fid, fact_b_id=fid, confidence=100, auto_resolved=True,
    )
    return {"fact_id": fid, "previous_value": value}


def delete_fact(*, fact_id: int) -> dict | None:
    """Hard-delete (= relation から完全削除)."""
    client = _cozo_client()
    row = client.run(
        "?[category, key, value] := *fact{id: $id, category, key, value} :limit 1",
        {"id": fact_id},
    ).get("rows", [])
    if not row:
        return None
    client.run("?[id] <- [[$id]] :rm fact {id}", {"id": fact_id})
    return {
        "fact_id": fact_id,
        "category": row[0][0],
        "key": row[0][1],
        "previous_value": row[0][2],
    }


# ── episode 系 ──────────────────────────────────────────────────────

def append_episode(
    *,
    session_id: str | None,
    role: str,
    content: str,
    summary: str | None,
) -> int:
    from scripts.db_cozo.repo import save_episode
    client = _cozo_client()
    sid = session_id or "mcp-append"
    return save_episode(client, role=role, content=content, session_id=sid)


def write_episode_embedding(episode_id: int, embedding_blob_or_list) -> None:
    """episode relation の embedding 列を更新."""
    import struct
    if isinstance(embedding_blob_or_list, (bytes, bytearray, memoryview)):
        vec = list(struct.unpack(f"{768}f", bytes(embedding_blob_or_list)))
    else:
        vec = list(embedding_blob_or_list)
    if not vec:
        return
    client = _cozo_client()
    row = client.run(
        "?[role, content, summary, session_id, topic_id, ts] := "
        "*episode{id: $id, role, content, summary, session_id, topic_id, "
        "timestamp: ts} :limit 1",
        {"id": episode_id},
    ).get("rows", [])
    if not row:
        return
    r = row[0]
    client.run(
        "?[id, role, content, summary, session_id, topic_id, timestamp, embedding] <- "
        "[[$id, $r, $c, $s, $sid, $tid, $ts, $emb]] "
        ":put episode {id => role, content, summary, session_id, "
        "topic_id, timestamp, embedding}",
        {"id": episode_id, "r": r[0], "c": r[1], "s": r[2],
         "sid": r[3], "tid": r[4], "ts": r[5], "emb": vec},
    )


def gc_episodes(
    *,
    raw_ttl_days: int = 30,
    summary_ttl_days: int | None = None,
) -> dict:
    """旧 raw_* role の episode を TTL 削除. 0.8.0 では role は user/assistant のみで
    raw_* は使わないため、 実質 no-op だが API 互換のために残す."""
    # 0.8.0: episode role は user/assistant のみ. raw_* / system 系は無い.
    return {"raw_deleted": 0, "summary_deleted": 0}


def bind_session_to_topic(*, session_id: str | None, topic_id: str) -> dict:
    """continue_topic 実装 (= session を既存 topic に紐付け)."""
    client = _cozo_client()
    # topic 存在確認
    exists = client.run(
        "?[id] := *topic{id}, id = $id :limit 1", {"id": topic_id},
    ).get("rows", [])
    if not exists:
        return {"error": f"no topic with id={topic_id}"}
    sid = session_id
    if not sid:
        # 最新 episode の session_id を流用
        row = client.run(
            "?[session_id, id] := *episode{id, session_id} :order -id :limit 1",
        ).get("rows", [])
        if not row:
            return {"error": "no session active (no episodes yet)"}
        sid = row[0][0]
    from scripts.db_cozo.repo import set_active_topic, touch_topic
    set_active_topic(client, sid, topic_id)
    touch_topic(client, topic_id)
    return {"bound": True, "session_id": sid, "topic_id": topic_id}


def search_episodes(*, embedding_blob: bytes, top_k: int) -> list[dict]:
    """episode をベクター検索."""
    import struct
    vec = list(struct.unpack(f"{768}f", bytes(embedding_blob)))
    client = _cozo_client()
    res = client.run(
        "?[dist, id, sid, role, summary, ts] := "
        "~episode:vec_idx{id, session_id: sid, role, summary, "
        "timestamp: ts | query: vec($q), k: $k, ef: 50, bind_distance: dist} "
        ":order dist",
        {"q": vec, "k": top_k},
    )
    rows = res.get("rows", [])
    return [
        {
            "id": r[1], "session_id": r[2], "role": r[3],
            "summary": r[4], "created_at": r[5], "distance": r[0],
        }
        for r in rows
    ]


# ── meta KV (set/get) ───────────────────────────────────────────────

def get_meta(key: str) -> str | None:
    from scripts.db_cozo.repo import get_meta as _g
    return _g(_cozo_client(), key)


def set_meta(key: str, value: str) -> None:
    from scripts.db_cozo.repo import set_meta as _s
    _s(_cozo_client(), key, value)


# ── legacy compatibility shim ───────────────────────────────────────
# 旧コードが `with connect() as c:` を使っていた箇所のための no-op shim.
# 0.8.0 では使われない. 残された呼出は段階的に削除.

@contextmanager
def connect():
    raise RuntimeError(
        "server/db.connect() は 0.8.0 で廃止されました. "
        "Cozo 経路 (_cozo_client) を使うか、 該当機能を Cozo backend で書き直してください."
    )
    yield  # unreachable
