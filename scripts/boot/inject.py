"""boot 層 (persona / rule) facts の取得 + 整形 + dirty フラグ管理.

仕様書 §8 (2 層構造) / §8.1 (即時反映) / §8.2 (容量管理).
"""
from __future__ import annotations

import sqlite3

from scripts.db.repo import get_meta, set_meta

BOOT_CATEGORIES = ("persona", "rule")
DIRTY_META_KEY = "boot_layer_dirty"

# §8.2 condense 提唱閾値 (暫定)
CONDENSE_FACTS_THRESHOLD = 50
CONDENSE_TOKENS_THRESHOLD = 5000


def fetch_boot_facts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT category, key, value, importance
        FROM facts
        WHERE category IN ('persona','rule') AND status='active'
        ORDER BY importance DESC, id ASC
        """,
    ).fetchall()
    return [
        {"category": r[0], "key": r[1], "value": r[2], "importance": r[3]}
        for r in rows
    ]


def format_boot_facts(facts: list[dict]) -> str:
    if not facts:
        return ""
    lines = ["## あなたの性格・本能 (boot 層)"]
    for f in facts:
        lines.append(f"- [{f['category']}/{f['key']}] {f['value']}")
    return "\n".join(lines)


def estimate_tokens(facts: list[dict]) -> int:
    """ざっくり token 数。日本語混在なので char/2 を上限近似として使う。"""
    total_chars = sum(len(f["value"]) + len(f["key"]) + len(f["category"]) + 8 for f in facts)
    return total_chars // 2


def condense_warning_if_needed(conn: sqlite3.Connection) -> str:
    """boot 層が閾値を超えていたら、警告文を返す。空文字 = 警告なし。"""
    facts = fetch_boot_facts(conn)
    n = len(facts)
    tokens = estimate_tokens(facts)
    if n > CONDENSE_FACTS_THRESHOLD or tokens > CONDENSE_TOKENS_THRESHOLD:
        return (
            f"⚠️  persona-memory: boot 層が肥大化しています "
            f"(facts={n}, ~tokens={tokens})。\n"
            f"   `/persona-memory:condense` で要約統合を検討してください。\n"
        )
    return ""


# ── dirty フラグ ─────────────────────────────────────────────────────────────

def mark_dirty(conn: sqlite3.Connection) -> None:
    """write 側が boot 層を更新したら呼ぶ。"""
    set_meta(conn, DIRTY_META_KEY, "1")


def is_dirty(conn: sqlite3.Connection) -> bool:
    return get_meta(conn, DIRTY_META_KEY) == "1"


def clear_dirty(conn: sqlite3.Connection) -> None:
    set_meta(conn, DIRTY_META_KEY, "0")
