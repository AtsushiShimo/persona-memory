"""lint LLM (judge_conflict) 主エントリ.

write 完了後の tail として detached child で起動される (= write_tail trigger).
**新 fact の近傍だけを対象とした limited lint**: 全 fact ペアの total lint は
SessionEnd 一括で v2.1 以降. MVP scope ではここまで.

設計判断 (2026-05-09 確定):
- 自動解消は confidence >= 90 のみ. 古い方 (id 小さい) を superseded に降格,
  source='lint_conflict' で印を付け, fact_embeddings から削除 (recall ノイズ排除).
- confidence 60-89 は conflicts テーブルに resolution='flagged' で記録のみ
  (両方 active のまま recall に出る). 人間レビュー / v2 の解消機構待ち.
- recall 側は supersede された fact の source='lint_conflict' を見て,
  active fact の補足として「以前は X と言っていたが撤回」 を 1 行添える.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from typing import Iterable

from scripts.db.connection import connect
from scripts.shared.embedding import pack
from scripts.shared.env import get_db_path
from scripts.shared.ollama import LLMClient, OllamaClient

JUDGE_MODEL = os.environ.get(
    "PERSONA_LINT_MODEL",
    os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b"),
)
EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
NEIGHBOR_TOP_K = int(os.environ.get("PERSONA_LINT_NEIGHBOR_TOP_K", "5"))
NEIGHBOR_DISTANCE_MAX = float(
    os.environ.get("PERSONA_LINT_DISTANCE_MAX", "0.5")
)
AUTO_RESOLVE_THRESHOLD = int(
    os.environ.get("PERSONA_LINT_AUTO_RESOLVE", "90")
)
FLAG_THRESHOLD = int(os.environ.get("PERSONA_LINT_FLAG", "60"))


_PROMPT = """\
2 つの記憶が論理的に矛盾しているか判定する。

記憶 A: {a}
記憶 B: {b}

判定基準:
- 同じ事柄について **互いに両立しない事実** を述べているか?
  (例: 『コーヒーは深煎り派』 vs 『コーヒーは浅煎りが好き』 = 矛盾)
- 異なる側面・補足・追記は矛盾ではない
  (例: 『コーヒーは深煎り』 vs 『砂糖は入れない』 = 別々の事実、両立)
- 抽象度の違う言い換えも矛盾ではない
  (例: 『犬が好き』 vs 『チワワを飼ってる』 = 両立)

confidence の目安:
  90-100 = ほぼ確実に矛盾
  60-89  = 矛盾の可能性高いが不確かさあり
  30-59  = 微妙、決め手に欠く
  0-29   = 矛盾していない

JSON のみ出力 (説明・前置き・コードフェンス禁止):
{{"contradict": true|false, "confidence": 0-100}}
"""


def judge_conflict(
    value_a: str, value_b: str, client: LLMClient,
    model: str = JUDGE_MODEL,
) -> tuple[bool, int]:
    """2 つの fact value が論理的に矛盾するか judge LLM で判定.

    戻り値: (contradict: bool, confidence: 0-100). LLM 失敗時は (False, 0).
    """
    prompt = _PROMPT.format(a=value_a, b=value_b)
    try:
        raw = client.generate(model, prompt)
    except Exception:
        return (False, 0)
    if not raw:
        return (False, 0)
    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE,
    )
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return (False, 0)
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return (False, 0)
    if not isinstance(data, dict):
        return (False, 0)
    contradict = bool(data.get("contradict", False))
    try:
        confidence = max(0, min(100, int(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    return (contradict, confidence)


def _fetch_fact(conn: sqlite3.Connection, fact_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, category, key, value, importance, status, source "
        "FROM facts WHERE id = ?",
        (fact_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "category": row[1], "key": row[2], "value": row[3],
        "importance": row[4], "status": row[5], "source": row[6],
    }


def _fetch_neighbors(
    conn: sqlite3.Connection, fact_id: int,
    embedding: list[float], top_k: int, distance_max: float,
) -> list[dict]:
    """active fact のうち、対象 fact 自身を除く近傍 top_k 件を取得."""
    blob = pack(embedding)
    rows = conn.execute(
        """
        WITH knn AS (
          SELECT fact_id, distance
          FROM fact_embeddings
          WHERE embedding MATCH ? AND k = ?
        )
        SELECT f.id, f.category, f.key, f.value, knn.distance
        FROM knn
        JOIN facts f ON f.id = knn.fact_id
        WHERE f.status = 'active' AND knn.fact_id != ?
        ORDER BY knn.distance
        """,
        (blob, top_k + 1, fact_id),  # +1 で自身分を吸収
    ).fetchall()
    return [
        {"id": r[0], "category": r[1], "key": r[2], "value": r[3], "distance": r[4]}
        for r in rows
        if r[4] <= distance_max
    ][:top_k]


def _auto_supersede(
    conn: sqlite3.Connection, older_id: int, newer_id: int,
) -> None:
    """古い方を superseded に降格. source='lint_conflict' でマーク,
    fact_embeddings からも削除 (recall ノイズ排除).

    supersedes / superseded_by の双方向リンクを張る.
    """
    conn.execute(
        "UPDATE facts SET status='superseded', "
        "  source='lint_conflict', "
        "  superseded_by=?, "
        "  updated_at=datetime('now', '+9 hours') "
        "WHERE id=?",
        (newer_id, older_id),
    )
    conn.execute(
        "UPDATE facts SET supersedes=? WHERE id=? AND supersedes IS NULL",
        (older_id, newer_id),
    )
    conn.execute("DELETE FROM fact_embeddings WHERE fact_id=?", (older_id,))


def _record_conflict(
    conn: sqlite3.Connection, a_id: int, b_id: int,
    confidence: int, resolution: str,
) -> None:
    conn.execute(
        "INSERT INTO conflicts(fact_a_id, fact_b_id, confidence, resolution) "
        "VALUES (?, ?, ?, ?)",
        (a_id, b_id, confidence, resolution),
    )


def _record_lint_run(
    conn: sqlite3.Connection,
    pairs_examined: int, flagged: int, auto_resolved: int,
    trigger: str = "write_tail",
) -> None:
    conn.execute(
        "INSERT INTO lint_log(pairs_examined, conflicts_flagged, "
        "  conflicts_auto_resolved, trigger_kind) "
        "VALUES (?, ?, ?, ?)",
        (pairs_examined, flagged, auto_resolved, trigger),
    )


def lint_around_fact(
    conn: sqlite3.Connection, fact_id: int, client: LLMClient,
    seen_pairs: set[tuple[int, int]] | None = None,
) -> dict:
    """1 fact の近傍に対して lint を実行.

    seen_pairs: 同 lint run 内で既判定 pair を skip するための共有 set.
    戻り値: {pairs_examined, flagged, auto_resolved}.
    """
    out = {"pairs_examined": 0, "flagged": 0, "auto_resolved": 0}
    if seen_pairs is None:
        seen_pairs = set()
    fact = _fetch_fact(conn, fact_id)
    if not fact or fact["status"] != "active":
        return out
    # value 単独ではなく `<cat>/<key>: <value>` を embed (recall と一致)
    embed_text = f"{fact['category']}/{fact['key']}: {fact['value']}"
    try:
        vec = client.embed(EMBED_MODEL, embed_text)
    except Exception:
        return out
    if not vec:
        return out
    neighbors = _fetch_neighbors(
        conn, fact_id, vec, NEIGHBOR_TOP_K, NEIGHBOR_DISTANCE_MAX,
    )
    for n in neighbors:
        pair = tuple(sorted((fact_id, n["id"])))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        # judge LLM 直前にもう一度 active 確認 (race 対策)
        cur_status = conn.execute(
            "SELECT status FROM facts WHERE id=?", (n["id"],),
        ).fetchone()
        if not cur_status or cur_status[0] != "active":
            continue
        contradict, confidence = judge_conflict(
            fact["value"], n["value"], client,
        )
        out["pairs_examined"] += 1
        if not contradict:
            continue
        older_id, newer_id = pair  # ソート済み = 小さい方が古い
        if confidence >= AUTO_RESOLVE_THRESHOLD:
            _auto_supersede(conn, older_id, newer_id)
            _record_conflict(conn, newer_id, older_id, confidence, "auto_superseded")
            out["auto_resolved"] += 1
        elif confidence >= FLAG_THRESHOLD:
            _record_conflict(conn, fact_id, n["id"], confidence, "flagged")
            out["flagged"] += 1
    conn.commit()
    return out


def run(
    fact_ids: Iterable[int], client: LLMClient | None = None,
    trigger: str = "write_tail",
) -> dict:
    """指定された fact_id 群の近傍 lint を一括実行.

    write の detached tail から呼ばれる想定.
    """
    db_path = get_db_path()
    if db_path is None:
        return {"error": "PERSONA_MEMORY_DB unset"}
    cli = client or OllamaClient()
    conn = connect(db_path)
    seen_pairs: set[tuple[int, int]] = set()
    totals = {"pairs_examined": 0, "flagged": 0, "auto_resolved": 0}
    try:
        for fid in fact_ids:
            try:
                r = lint_around_fact(conn, fid, cli, seen_pairs)
                for k in totals:
                    totals[k] += r[k]
            except Exception as e:
                sys.stderr.write(f"[persona-memory] lint fact_id={fid} failed: {e}\n")
                continue
        _record_lint_run(
            conn,
            pairs_examined=totals["pairs_examined"],
            flagged=totals["flagged"],
            auto_resolved=totals["auto_resolved"],
            trigger=trigger,
        )
        conn.commit()
    finally:
        conn.close()
    return totals


def main() -> int:
    """stdin: {"fact_ids": [...], "trigger": "write_tail"|"manual"}."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    fact_ids = payload.get("fact_ids") or []
    if not fact_ids:
        return 0
    trigger = payload.get("trigger") or "write_tail"
    run(fact_ids, trigger=trigger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
