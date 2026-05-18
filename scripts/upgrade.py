"""/persona-memory:upgrade — boot 層 default refresh + config.env 相対パス化.

業務フローは Cozo 単独 (= server / hook / write / lint / recall) のため、
upgrade の仕事は 2 つに簡略化された:

1. **Cozo boot fact refresh**:
   `scripts.boot.defaults.DEFAULT_BOOT_FACTS` の最新版を Cozo に idempotent
   upsert. 値が変わっていれば update, 廃止 default (DEPRECATED_BOOT_FACTS)
   は status='superseded' に降格.

2. **config.env の相対パス化** (= ペルソナディレクトリの可搬性確保).

ペルソナ固有属性 (role / identity / personality / etc.) は触らない.

0.8.2 で旧 SQLite → Cozo migrate 経路を削除. SQLite ペルソナを抱えている
ユーザーは 0.8.1 までに /persona-memory:upgrade で移行を済ませている前提.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from scripts.boot.defaults import (
    DEFAULT_BOOT_FACTS, DEPRECATED_BOOT_FACTS,
)
from scripts.db_cozo.connection import init_db, next_id
from scripts.shared.ollama import OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")


def _now_jst() -> str:
    import datetime as dt
    tz = dt.timezone(dt.timedelta(hours=9))
    return dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def _upsert_boot_fact_cozo(
    client, category: str, key: str, value: str, importance: int,
    embedding: list[float] | None = None, source: str = "upgrade",
) -> tuple[int, str]:
    """boot fact を Cozo に upsert. 戻り値 (fact_id, action) で action は
    'unchanged' / 'updated' / 'inserted'."""
    ts = _now_jst()
    row = client.run(
        "?[id, value, importance] := *fact{id, category, key, value, "
        "importance, status: 'active'}, "
        "category = $c, key = $k :limit 1",
        {"c": category, "k": key},
    ).get("rows", [])
    if row:
        fid, cur_val, cur_imp = row[0][0], row[0][1], row[0][2]
        if cur_val == value and cur_imp == importance:
            return fid, "unchanged"
        # update (= 値変更)
        action = "updated"
    else:
        fid = next_id(client, "fact")
        action = "inserted"
    fields = {
        "id": fid, "c": category, "k": key, "v": value, "imp": importance,
        "ts": ts, "src": source,
    }
    if embedding:
        fields["emb"] = embedding
        client.run(
            "?[id, category, key, value, importance, status, source, "
            "created_at, updated_at, access_count, embedding] <- "
            "[[$id, $c, $k, $v, $imp, 'active', $src, $ts, $ts, 0, $emb]] "
            ":put fact {id => category, key, value, importance, status, "
            "source, created_at, updated_at, access_count, embedding}",
            fields,
        )
    else:
        client.run(
            "?[id, category, key, value, importance, status, source, "
            "created_at, updated_at, access_count] <- "
            "[[$id, $c, $k, $v, $imp, 'active', $src, $ts, $ts, 0]] "
            ":put fact {id => category, key, value, importance, status, "
            "source, created_at, updated_at, access_count}",
            fields,
        )
    return fid, action


def _deprecate_boot_fact_cozo(client, category: str, key: str) -> bool:
    """active な廃止 default を status='superseded' に降格. 影響行数 bool."""
    row = client.run(
        "?[id, value, importance, source, created_at, ac, sup, supby, "
        "rs, la, emb] := "
        "*fact{id, category, key, value, importance, status: 'active', "
        "source, created_at, access_count: ac, supersedes: sup, "
        "superseded_by: supby, reason_superseded: rs, "
        "last_accessed_at: la, embedding: emb}, "
        "category = $c, key = $k :limit 1",
        {"c": category, "k": key},
    ).get("rows", [])
    if not row:
        return False
    r = row[0]
    ts = _now_jst()
    client.run(
        "?[id, category, key, value, importance, access_count, status, "
        "supersedes, superseded_by, source, reason_superseded, "
        "created_at, updated_at, last_accessed_at, embedding] <- "
        "[[$id, $c, $k, $v, $imp, $ac, 'superseded', $sup, $supby, "
        "$src, $rs, $cr, $ts, $la, $emb]] "
        ":put fact {id => category, key, value, importance, access_count, "
        "status, supersedes, superseded_by, source, reason_superseded, "
        "created_at, updated_at, last_accessed_at, embedding}",
        {"id": r[0], "c": category, "k": key, "v": r[1], "imp": r[2],
         "src": r[3], "cr": r[4], "ac": r[5], "sup": r[6], "supby": r[7],
         "rs": r[8], "la": r[9], "emb": r[10], "ts": ts},
    )
    return True


def _migrate_config_env_to_relative(cozo_path: Path) -> str:
    """旧 config.env の絶対パスを ${CLAUDE_PROJECT_DIR} 基準に書き換え.

    戻り値: 'migrated' / 'already' / 'skipped'.
    """
    persona = cozo_path.stem
    if persona.endswith(".cozo"):
        persona = persona[: -len(".cozo")]
    config_env = cozo_path.parent / f"{persona}.config.env"
    if not config_env.exists():
        return "skipped"
    text = config_env.read_text(encoding="utf-8")
    if "${CLAUDE_PROJECT_DIR}" in text or "$CLAUDE_PROJECT_DIR" in text:
        return "already"
    # 絶対パス形式を相対形式に書き換え
    pattern = re.compile(
        r"^(PERSONA_MEMORY_DB=)(?:'|\")?/[^'\"\n]+/(\.persona-memory/[^'\"\n]+\.db)(?:'|\")?$",
        re.MULTILINE,
    )
    if pattern.search(text):
        new_text = pattern.sub(
            r"\1${CLAUDE_PROJECT_DIR}/\2", text,
        )
        config_env.write_text(new_text, encoding="utf-8")
        return "migrated"
    return "skipped"


def upgrade(cozo_path: Path) -> dict[str, int]:
    """boot 層 default refresh + config.env 相対パス化.

    cozo_path: `<persona>.cozo.db` への絶対 / 相対パス.
    """
    counts = {
        "inserted": 0, "updated": 0, "unchanged": 0, "deprecated": 0,
        "config_env_migrated": 0,
    }

    # 1. Cozo boot fact refresh
    client = init_db(cozo_path)
    llm = OllamaClient()

    for category, key in DEPRECATED_BOOT_FACTS:
        if _deprecate_boot_fact_cozo(client, category, key):
            counts["deprecated"] += 1
            print(f"  deprecated [{category}/{key}]")

    for category, key, value, importance in DEFAULT_BOOT_FACTS:
        try:
            vec = llm.embed(EMBED_MODEL, f"{category}/{key}: {value}")
        except Exception:
            vec = []
        fid, action = _upsert_boot_fact_cozo(
            client, category, key, value, importance,
            embedding=vec or None,
        )
        counts[action] = counts.get(action, 0) + 1
        if action == "inserted":
            print(f"  inserted [{category}/{key}]")
        elif action == "updated":
            print(f"  updated [{category}/{key}]")

    # 2. config.env 相対パス化
    cfg_status = _migrate_config_env_to_relative(cozo_path)
    if cfg_status == "migrated":
        counts["config_env_migrated"] = 1
        print("  migrated config.env to ${CLAUDE_PROJECT_DIR}")

    return counts


def main() -> None:
    p = argparse.ArgumentParser(description="Refresh boot facts + relative-path config")
    p.add_argument("--db", type=Path, default=None,
                   help="Cozo DB ファイルパス (未指定時は PERSONA_MEMORY_DB env)")
    args = p.parse_args()

    db_path = args.db
    if db_path is None:
        env_db = os.environ.get("PERSONA_MEMORY_DB", "").strip()
        if not env_db:
            print("ERROR: --db か PERSONA_MEMORY_DB env のどちらかが必要です", file=sys.stderr)
            sys.exit(2)
        db_path = Path(env_db)

    # 旧 SQLite path (`<persona>.db`) を受けても `.cozo.db` に振り直す.
    # 0.8.2 で SQLite 経路を廃止したため、 caller がまだ旧 path を渡してきた
    # 場合の救済 (path-only 救済 = 実 SQLite ファイルからは読まない).
    if db_path.suffix == ".db" and not db_path.name.endswith(".cozo.db"):
        db_path = db_path.with_suffix(".cozo.db")

    if not db_path.exists():
        print(f"ERROR: Cozo DB not found: {db_path}", file=sys.stderr)
        print(
            "  /persona-memory:init で新規ペルソナを作成してください.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"=== boot 層 default を refresh ===")
    counts = upgrade(db_path)
    print(f"=== 完了: {counts} ===")


if __name__ == "__main__":
    main()
