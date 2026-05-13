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
# num_ctx を recall / write と揃えて Ollama 単一インスタンス共有 (詳細は recall.extract).
LINT_NUM_CTX = int(os.environ.get("PERSONA_LINT_NUM_CTX", "16384"))
EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
NEIGHBOR_TOP_K = int(os.environ.get("PERSONA_LINT_NEIGHBOR_TOP_K", "5"))
# 0.5 では過剰検出 (異 category / 別属性同士でも heavy LLM が「矛盾」 と
# 高 confidence で誤判定するケース多発). 0.4 に絞り、かつ _fetch_neighbors で
# 同 category 制限を加えた (異 category 間で矛盾はあり得ない設計判断).
NEIGHBOR_DISTANCE_MAX = float(
    os.environ.get("PERSONA_LINT_DISTANCE_MAX", "0.4")
)
AUTO_RESOLVE_THRESHOLD = int(
    os.environ.get("PERSONA_LINT_AUTO_RESOLVE", "90")
)
FLAG_THRESHOLD = int(os.environ.get("PERSONA_LINT_FLAG", "60"))


_PROMPT = """\
2 つの記憶が **同じ事柄について論理的に矛盾している** か判定する。

記憶 A: {a}
記憶 B: {b}

## 判定基準 (厳格)

**矛盾と判定する条件 (両方を満たす時のみ)**:
1. 同じ対象 / 同じ属性について述べている (例: 同一人物の同一の好み、
   同じ物の同じ性質、同じ事実の同じ側面)
2. その属性について **両立しない値** が示されている

**矛盾と判定してはいけないパターン**:
- 異なる属性 / 異なる側面: 「コーヒーは深煎り」 と「砂糖なし」 は別属性 → 両立
- 異なる対象: 「犬が好き」 と「猫が嫌い」 は別対象 → 両立
- 抽象度の差: 「犬を飼ってる」 と「チワワを飼ってる」 → 包含関係 = 両立
- 補足・追記: 「コーヒーは深煎り」 と「コーヒーはブラックで飲む」 → 両立
- 時系列での自然な変化: 一般情報の更新は矛盾ではなく単なる更新

**矛盾の典型例 (= true 判定)**:
- 「コーヒーは深煎り派」 vs 「コーヒーは浅煎りが好き」 (同属性で対立)
- 「猫を飼ってる」 vs 「ペットはいない」 (同事実で対立)
- 「主に Python を書く」 vs 「Python は書かない」 (同行動で対立)

## confidence の目安

- 90-100 = 同事柄について明確に対立する値が両方にあり、不一致が動かしようがない
- 60-89  = 対立しているが、表現が曖昧で別解釈の余地がある
- 30-59  = どちらかの属性が違う / 同事柄か疑わしい
- 0-29   = 同事柄ではない / 両立可能 / 補足関係

## 出力

JSON のみ (説明・前置き・コードフェンス禁止):
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
        raw = client.generate(model, prompt, num_ctx=LINT_NUM_CTX)
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
    conn: sqlite3.Connection, fact_id: int, category: str, key: str,
    embedding: list[float], top_k: int, distance_max: float,
) -> list[dict]:
    """active fact のうち、起点 fact と **同 category かつ同属性** で自身を除く近傍 top_k 件.

    同属性判定は `scripts.write.similarity._is_same_attribute` (= key 末尾単語の一致)
    に倣う. 例: 起点 key='pet_dog_name' なら近傍 key='pet_dog_name' / 'foo_name' は
    judge 対象、 'pet_dog_breed' / 'pet_dog_gender' は別属性なので除外.

    0.5.16 で同 category 制限を入れたが, 同 category 内でも異属性 fact 同士を
    judge LLM が高 confidence で「矛盾」 と誤判定する事象 (例: pet_dog_name=
    まろん vs pet_dog_breed=ミニチュアダックスフンド を 95% で矛盾と判定) が
    観測されたため, 0.5.17 で属性 (= key 末尾単語) の一致も要求する.
    """
    # 循環 import 回避のためここで import
    from scripts.write.similarity import _is_same_attribute

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
        WHERE f.status = 'active'
          AND f.category = ?
          AND knn.fact_id != ?
        ORDER BY knn.distance
        """,
        (blob, top_k + 5, category, fact_id),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        if r[4] > distance_max:
            continue
        if not _is_same_attribute(r[2], key):
            continue
        out.append(
            {"id": r[0], "category": r[1], "key": r[2], "value": r[3], "distance": r[4]}
        )
        if len(out) >= top_k:
            break
    return out


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

    # ------- Phase A: read (短時間 conn 使用) -------
    fact = _fetch_fact(conn, fact_id)
    if not fact or fact["status"] != "active":
        return out

    # ------- Phase B: embed LLM (DB に touch しない) -------
    # value 単独ではなく `<cat>/<key>: <value>` を embed (recall と一致)
    embed_text = f"{fact['category']}/{fact['key']}: {fact['value']}"
    try:
        vec = client.embed(EMBED_MODEL, embed_text)
    except Exception:
        return out
    if not vec:
        return out

    # ------- Phase A2: 近傍 read (短時間 conn 使用) -------
    neighbors = _fetch_neighbors(
        conn, fact_id, fact["category"], fact["key"], vec,
        NEIGHBOR_TOP_K, NEIGHBOR_DISTANCE_MAX,
    )

    # ------- Phase B2 & C: judge LLM ごとに 1 conn touch -------
    # 0.6.10 で transaction 短縮: judge LLM (10-30s/件) は conn を経由せず実行し、
    # 結果が出てから _fetch_active_status / _auto_supersede / _record_conflict を
    # 短時間 で実行する。これにより lint 中の他プロセス write がブロックされない。
    for n in neighbors:
        pair = tuple(sorted((fact_id, n["id"])))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        # judge LLM (DB touch なし)
        contradict, confidence = judge_conflict(
            fact["value"], n["value"], client,
        )
        out["pairs_examined"] += 1
        if not contradict:
            continue
        # 短時間 conn touch: race 対策の active 確認 → write
        cur_status = conn.execute(
            "SELECT status FROM facts WHERE id=?", (n["id"],),
        ).fetchone()
        if not cur_status or cur_status[0] != "active":
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
