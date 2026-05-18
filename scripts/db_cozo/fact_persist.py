"""fact 候補を Cozo に永続化 (補強 / 変更 / 新規挿入).

旧 scripts/write/persist.py の Cozo 版.
SQLite と Cozo は **独立に** fact id を採番するため、 SQLite 側で計算した
Match の fact_id を Cozo に転用できない. Cozo 側で再度 find_match して
独立の supersede chain を維持する.

設計:
- 0.7.4 までは SQLite との **並列保存** 経路.
- SessionStart の boot 注入は cozo_db_present 時に Cozo から fetch する
  (boot/inject_cozo.py).
- lint も同様 Cozo に向ける.
- SQLite 側は backup として並走 (= 削除しない).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from pycozo.client import Client

from scripts.boot.defaults import PROTECTED_KEYS
from scripts.db_cozo.connection import next_id
from scripts.write.extract import FactCandidate
from scripts.write.similarity import (
    EMBED_DISTANCE_MAX, _is_same_attribute, is_reinforcement,
)

BOOT_CATEGORIES = ("persona", "rule")


@dataclass
class CozoMatch:
    fact_id: int
    category: str
    key: str
    value: str
    importance: int
    method: str  # 'key' or 'embedding'


def _now_jst() -> str:
    """JST timestamp (= SQLite の datetime('now', '+9 hours') と整合)."""
    tz = dt.timezone(dt.timedelta(hours=9))
    return dt.datetime.now(tz).replace(tzinfo=None).isoformat(
        sep=" ", timespec="seconds",
    )


# ── find_match (Cozo) ───────────────────────────────────────────────────

def find_by_key(client: Client, category: str, key: str) -> CozoMatch | None:
    res = client.run(
        "?[id, category, key, value, importance] := "
        "*fact{id, category, key, value, importance, status: 'active'}, "
        "category = $cat, key = $key",
        {"cat": category, "key": key},
    )
    rows = res.get("rows", [])
    if not rows:
        return None
    r = rows[0]
    return CozoMatch(
        fact_id=r[0], category=r[1], key=r[2],
        value=r[3], importance=r[4], method="key",
    )


def find_by_embedding(
    client: Client, category: str, embedding: list[float],
    distance_max: float = EMBED_DISTANCE_MAX, k: int = 10,
) -> CozoMatch | None:
    """同 category 内の active fact で embedding 近傍 1 件."""
    if not embedding:
        return None
    res = client.run(
        "?[dist, id, category, key, value, importance] := "
        "~fact:vec_idx{id, category, key, value, importance | "
        "query: vec($q), k: $k, ef: 50, bind_distance: dist}, "
        "*fact{id, status: 'active'}, "
        "category = $cat, dist < $dmax "
        ":order dist :limit 1",
        {"q": embedding, "k": k, "dmax": distance_max, "cat": category},
    )
    rows = res.get("rows", [])
    if not rows:
        return None
    r = rows[0]
    return CozoMatch(
        fact_id=r[1], category=r[2], key=r[3],
        value=r[4], importance=r[5], method="embedding",
    )


def find_match(
    client: Client, category: str, key: str,
    embedding: list[float] | None,
    distance_max: float = EMBED_DISTANCE_MAX,
) -> CozoMatch | None:
    m = find_by_key(client, category, key)
    if m:
        return m
    if embedding:
        cand = find_by_embedding(client, category, embedding, distance_max)
        if cand and _is_same_attribute(cand.key, key):
            return cand
    return None


# ── fact 行操作 (insert / reinforce / supersede) ─────────────────────────

def _put_fact(client: Client, row: dict) -> None:
    """fact 行の全 column を埋めて put (= upsert).

    embedding は値があれば vec() で wrap して schema に含め, None なら
    schema 自体から除外 (= default null).
    """
    params = {
        "id": row["id"], "cat": row["category"], "key": row["key"],
        "val": row["value"], "imp": row["importance"],
        "ac": row.get("access_count", 0),
        "st": row.get("status", "active"),
        "sup": row.get("supersedes"),
        "supby": row.get("superseded_by"),
        "src": row.get("source"),
        "rs": row.get("reason_superseded"),
        "ca": row.get("created_at") or _now_jst(),
        "ua": row.get("updated_at") or _now_jst(),
        "la": row.get("last_accessed_at"),
    }
    emb = row.get("embedding")
    if emb is not None and len(emb) > 0:
        client.run(
            "?[id, category, key, value, importance, access_count, status, "
            "supersedes, superseded_by, source, reason_superseded, "
            "created_at, updated_at, last_accessed_at, embedding] <- "
            "[[$id, $cat, $key, $val, $imp, $ac, $st, $sup, $supby, $src, $rs, "
            "$ca, $ua, $la, vec($emb)]] "
            ":put fact {id => category, key, value, importance, access_count, "
            "status, supersedes, superseded_by, source, reason_superseded, "
            "created_at, updated_at, last_accessed_at, embedding}",
            {**params, "emb": list(emb)},
        )
    else:
        client.run(
            "?[id, category, key, value, importance, access_count, status, "
            "supersedes, superseded_by, source, reason_superseded, "
            "created_at, updated_at, last_accessed_at] <- "
            "[[$id, $cat, $key, $val, $imp, $ac, $st, $sup, $supby, $src, $rs, "
            "$ca, $ua, $la]] "
            ":put fact {id => category, key, value, importance, access_count, "
            "status, supersedes, superseded_by, source, reason_superseded, "
            "created_at, updated_at, last_accessed_at}",
            params,
        )


def _read_fact(client: Client, fact_id: int) -> dict | None:
    res = client.run(
        "?[id, category, key, value, importance, access_count, status, "
        "supersedes, superseded_by, source, reason_superseded, "
        "created_at, updated_at, last_accessed_at, embedding] := "
        "*fact{id, category, key, value, importance, access_count, status, "
        "supersedes, superseded_by, source, reason_superseded, "
        "created_at, updated_at, last_accessed_at, embedding}, id = $id",
        {"id": fact_id},
    )
    rows = res.get("rows", [])
    if not rows:
        return None
    cols = ["id", "category", "key", "value", "importance", "access_count",
            "status", "supersedes", "superseded_by", "source",
            "reason_superseded", "created_at", "updated_at",
            "last_accessed_at", "embedding"]
    return dict(zip(cols, rows[0]))


def insert_new(
    client: Client,
    candidate: FactCandidate,
    embedding: list[float] | None,
    source: str | None = None,
    supersedes: int | None = None,
) -> int:
    fact_id = next_id(client, "fact")
    _put_fact(client, {
        "id": fact_id,
        "category": candidate.category,
        "key": candidate.key,
        "value": candidate.value,
        "importance": candidate.importance,
        "access_count": 0,
        "status": "active",
        "supersedes": supersedes,
        "superseded_by": None,
        "source": source,
        "reason_superseded": None,
        "created_at": _now_jst(),
        "updated_at": _now_jst(),
        "last_accessed_at": None,
        "embedding": embedding if embedding else None,
    })
    return fact_id


def reinforce(
    client: Client, match: CozoMatch, candidate: FactCandidate,
) -> None:
    """既存 fact の value 更新 + importance/access_count +1."""
    existing = _read_fact(client, match.fact_id)
    if existing is None:
        return
    new_importance = min(9, max(match.importance, candidate.importance) + 1)
    existing.update({
        "value": candidate.value,
        "importance": new_importance,
        "access_count": existing.get("access_count", 0) + 1,
        "updated_at": _now_jst(),
    })
    _put_fact(client, existing)


def _rm_fact(client: Client, fact_id: int) -> None:
    client.run(
        "?[id] <- [[$id]] :rm fact {id}",
        {"id": fact_id},
    )


def supersede(
    client: Client,
    match: CozoMatch,
    candidate: FactCandidate,
    embedding: list[float] | None,
    source: str | None = None,
    reason: str | None = None,
) -> int:
    """旧 fact を superseded 降格 → 新 fact insert → 旧の superseded_by 更新.

    HNSW index の不整合を避けるため、 旧 row は :rm で完全削除してから
    embedding=null の状態で同 id で再 put する.
    """
    old = _read_fact(client, match.fact_id)
    if old is None:
        return -1
    _rm_fact(client, match.fact_id)
    old.update({
        "status": "superseded",
        "reason_superseded": reason,
        "embedding": None,
        "updated_at": _now_jst(),
    })
    _put_fact(client, old)
    new_id = insert_new(
        client, candidate, embedding, source=source, supersedes=match.fact_id,
    )
    # 旧の superseded_by を新 id に
    old2 = _read_fact(client, match.fact_id)
    if old2 is not None:
        _rm_fact(client, match.fact_id)
        old2["superseded_by"] = new_id
        old2["updated_at"] = _now_jst()
        _put_fact(client, old2)
    return new_id


# ── dirty フラグ (boot 層変更通知) ──────────────────────────────────────

def mark_boot_dirty(client: Client) -> None:
    client.run(
        "?[key, value] <- [['boot_dirty', '1']] :put meta {key => value}",
    )


def is_boot_dirty(client: Client) -> bool:
    res = client.run(
        "?[v] := *meta{key: 'boot_dirty', value: v}",
    )
    rows = res.get("rows", [])
    return bool(rows) and rows[0][0] == "1"


def clear_boot_dirty(client: Client) -> None:
    client.run(
        "?[key, value] <- [['boot_dirty', '0']] :put meta {key => value}",
    )


# ── apply_candidate (= persist.py:apply_candidate の Cozo 版) ──────────

def apply_candidate(
    client: Client,
    candidate: FactCandidate,
    match: CozoMatch | None,
    embedding: list[float] | None,
    source: str | None = None,
    reason: str | None = None,
) -> str:
    """1 候補に対して 補強 / 変更 / 新規挿入 のいずれかを適用.

    Returns: "reinforce" / "supersede" / "insert" / "protected"
    """
    if (candidate.category, candidate.key) in PROTECTED_KEYS:
        return "protected"
    if match is None:
        insert_new(client, candidate, embedding, source)
        if candidate.category in BOOT_CATEGORIES:
            mark_boot_dirty(client)
        return "insert"
    if is_reinforcement(match.value, candidate.value):
        reinforce(client, match, candidate)
        if candidate.category in BOOT_CATEGORIES:
            mark_boot_dirty(client)
        return "reinforce"
    if match.method == "embedding" and match.key != candidate.key:
        candidate.key = match.key
    supersede(client, match, candidate, embedding, source, reason=reason)
    if candidate.category in BOOT_CATEGORIES:
        mark_boot_dirty(client)
    return "supersede"


# ── boot 層 fact 取得 (Cozo 版) ────────────────────────────────────────

def fetch_boot_facts(client: Client) -> list[dict]:
    """boot 層 (persona / rule) で active かつ playbook_ プレフィクス除外の facts.

    boot/inject.fetch_boot_facts の Cozo 版.
    """
    res = client.run(
        "?[category, key, value, importance] := "
        "*fact{category, key, value, importance, status: 'active'}, "
        "category in ['persona', 'rule'], "
        "!starts_with(key, 'playbook_') "
        ":order category, -importance, key",
        {},
    )
    return [
        {"category": r[0], "key": r[1], "value": r[2], "importance": r[3]}
        for r in res.get("rows", [])
    ]


def fetch_facts_by_ids(client: Client, fact_ids: list[int]) -> list[dict]:
    if not fact_ids:
        return []
    res = client.run(
        "?[id, category, key, value, importance, status, "
        "supersedes, superseded_by, reason_superseded] := "
        "*fact{id, category, key, value, importance, status, "
        "supersedes, superseded_by, reason_superseded}, "
        "id in $ids",
        {"ids": fact_ids},
    )
    out = []
    for r in res.get("rows", []):
        out.append({
            "id": r[0], "category": r[1], "key": r[2], "value": r[3],
            "importance": r[4], "status": r[5],
            "supersedes": r[6], "superseded_by": r[7],
            "reason_superseded": r[8],
        })
    return out
