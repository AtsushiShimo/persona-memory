"""boot 層 facts の取得 + 整形 + dirty フラグ管理 (0.8.0 — Cozo 単独経路).

format_boot_facts は Cozo / SQLite 共通の表示ロジックなので残置.
BOOT_CATEGORIES / DIRTY_META_KEY 定数も他モジュール (= fact_persist) から
参照されるため維持. SQLite 直接呼び出しは全削除.
"""
from __future__ import annotations

BOOT_CATEGORIES = ("persona", "rule")
DIRTY_META_KEY = "boot_layer_dirty"

# §8.2 condense 提唱閾値 (暫定)
CONDENSE_FACTS_THRESHOLD = 50
CONDENSE_TOKENS_THRESHOLD = 5000


def format_boot_facts(facts: list[dict]) -> str:
    if not facts:
        return ""
    lines = ["## あなたの性格・本能 (boot 層)"]
    for f in facts:
        lines.append(f"- [{f['category']}/{f['key']}] {f['value']}")
    return "\n".join(lines)


def estimate_tokens(facts: list[dict]) -> int:
    """ざっくり token 数 (char/2 を上限近似)."""
    total = sum(
        len(f["value"]) + len(f["key"]) + len(f["category"]) + 8
        for f in facts
    )
    return total // 2


def condense_warning_if_needed_cozo(client) -> str:
    """Cozo 版: boot 層が肥大化していたら警告文を返す."""
    from scripts.db_cozo.fact_persist import fetch_boot_facts
    facts = fetch_boot_facts(client)
    n = len(facts)
    tokens = estimate_tokens(facts)
    if n > CONDENSE_FACTS_THRESHOLD or tokens > CONDENSE_TOKENS_THRESHOLD:
        return (
            f"⚠️  persona-memory: boot 層が肥大化しています "
            f"(facts={n}, ~tokens={tokens})。\n"
            f"   `/persona-memory:condense` で要約統合を検討してください。\n"
        )
    return ""
