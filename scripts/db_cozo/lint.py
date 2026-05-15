"""lint (矛盾検査) の Cozo 版.

旧 scripts/lint/run.py の Cozo 移植. lint LLM (gemma3:12b) の判定ロジック
本体 (= judge_conflict / parse_response 等) は SQLite 版の関数をそのまま使い、
DB 操作部 (fetch / neighbors / supersede / record) のみ Cozo 化する.

呼び出しタイミングは write tail (= write 完了後の detached spawn). 既存
SQLite 経路と並走させ、 SQLite と Cozo は独立に supersede chain と conflict
record を持つ.
"""
from __future__ import annotations

import datetime as dt

from pycozo.client import Client

from scripts.db_cozo.connection import next_id
from scripts.db_cozo.fact_persist import _put_fact, _read_fact, _rm_fact
from scripts.shared.ollama import LLMClient


def _now_jst() -> str:
    tz = dt.timezone(dt.timedelta(hours=9))
    return dt.datetime.now(tz).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds",
    )


def fetch_fact(client: Client, fact_id: int) -> dict | None:
    row = _read_fact(client, fact_id)
    if not row:
        return None
    return {
        "id": row["id"], "category": row["category"], "key": row["key"],
        "value": row["value"], "importance": row["importance"],
        "status": row["status"], "source": row.get("source"),
    }


def fetch_neighbors(
    client: Client, fact_id: int, category: str, key: str,
    embedding: list[float], top_k: int, distance_max: float,
) -> list[dict]:
    """起点 fact と同 category かつ同属性 (= key 末尾単語一致) の active 近傍."""
    from scripts.write.similarity import _is_same_attribute

    if not embedding:
        return []
    res = client.run(
        "?[dist, id, category, key, value] := "
        "~fact:vec_idx{id, category, key, value | "
        "query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
        "*fact{id, status: 'active'}, "
        "category = $cat, id != $self, dist < $dmax "
        ":order dist",
        {"q": embedding, "k": top_k + 5, "cat": category,
         "self": fact_id, "dmax": distance_max},
    )
    out: list[dict] = []
    for r in res.get("rows", []):
        if not _is_same_attribute(r[3], key):
            continue
        out.append({
            "id": r[1], "category": r[2], "key": r[3], "value": r[4],
            "distance": r[0],
        })
        if len(out) >= top_k:
            break
    return out


def auto_supersede(client: Client, older_id: int, newer_id: int) -> None:
    """古い fact を superseded 降格 (source=lint_conflict, embedding 消去).

    旧 row を :rm → embedding=None, status='superseded', superseded_by=newer
    で再 put. newer 側で supersedes が未設定なら older にセット.
    """
    older = _read_fact(client, older_id)
    if older is None:
        return
    _rm_fact(client, older_id)
    older.update({
        "status": "superseded",
        "source": "lint_conflict",
        "superseded_by": newer_id,
        "embedding": None,
        "updated_at": _now_jst(),
    })
    _put_fact(client, older)
    newer = _read_fact(client, newer_id)
    if newer is not None and newer.get("supersedes") is None:
        _rm_fact(client, newer_id)
        newer["supersedes"] = older_id
        newer["updated_at"] = _now_jst()
        _put_fact(client, newer)


def record_conflict(
    client: Client, a_id: int, b_id: int,
    confidence: int, resolution: str,
) -> int:
    cid = next_id(client, "conflict")
    ts = _now_jst()
    client.run(
        "?[id, fact_a_id, fact_b_id, confidence, resolution, detected_at] <- "
        "[[$id, $a, $b, $c, $r, $ts]] "
        ":put conflict {id => fact_a_id, fact_b_id, confidence, resolution, "
        "detected_at}",
        {"id": cid, "a": a_id, "b": b_id, "c": confidence,
         "r": resolution, "ts": ts},
    )
    return cid


def record_lint_run(
    client: Client, pairs_examined: int, flagged: int,
    auto_resolved: int, trigger: str = "write_tail",
) -> int:
    lid = next_id(client, "lint_log")
    ts = _now_jst()
    client.run(
        "?[id, run_at, pairs_examined, conflicts_flagged, "
        "conflicts_auto_resolved, trigger_kind] <- "
        "[[$id, $ts, $pe, $fl, $ar, $tk]] "
        ":put lint_log {id => run_at, pairs_examined, conflicts_flagged, "
        "conflicts_auto_resolved, trigger_kind}",
        {"id": lid, "ts": ts, "pe": pairs_examined, "fl": flagged,
         "ar": auto_resolved, "tk": trigger},
    )
    return lid


def lint_around_fact_cozo(
    client: Client, fact_id: int, llm_client: LLMClient,
    seen_pairs: set[tuple[int, int]] | None = None,
    embed_model: str | None = None,
    neighbor_top_k: int = 5,
    neighbor_distance_max: float = 0.4,
    auto_resolve_threshold: int = 85,
    flag_threshold: int = 70,
) -> dict:
    """1 fact の近傍に対して lint を実行 (Cozo 版).

    判定 LLM (judge_conflict) は SQLite 版と完全に共有.
    """
    import os
    from scripts.lint.run import judge_conflict

    if embed_model is None:
        embed_model = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
    if seen_pairs is None:
        seen_pairs = set()
    out = {"pairs_examined": 0, "flagged": 0, "auto_resolved": 0}
    fact = fetch_fact(client, fact_id)
    if not fact or fact["status"] != "active":
        return out
    embed_text = f"{fact['category']}/{fact['key']}: {fact['value']}"
    try:
        vec = llm_client.embed(embed_model, embed_text)
    except Exception:
        return out
    if not vec:
        return out
    neighbors = fetch_neighbors(
        client, fact_id, fact["category"], fact["key"], vec,
        neighbor_top_k, neighbor_distance_max,
    )
    for n in neighbors:
        pair = tuple(sorted((fact_id, n["id"])))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        contradict, confidence = judge_conflict(
            fact["value"], n["value"], llm_client,
        )
        out["pairs_examined"] += 1
        if not contradict:
            continue
        # race 対策: judge 後に active 確認
        cur = _read_fact(client, n["id"])
        if not cur or cur.get("status") != "active":
            continue
        older_id, newer_id = pair
        if confidence >= auto_resolve_threshold:
            auto_supersede(client, older_id, newer_id)
            record_conflict(
                client, newer_id, older_id, confidence, "auto_superseded",
            )
            out["auto_resolved"] += 1
        elif confidence >= flag_threshold:
            record_conflict(
                client, fact_id, n["id"], confidence, "flagged",
            )
            out["flagged"] += 1
    return out
