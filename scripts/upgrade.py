#!/usr/bin/env python3
"""プラグイン更新後の non-destructive な boot 層 default refresh.

`/persona-memory:upgrade` から呼ばれる。idempotent:
- DEFAULT_BOOT_FACTS の各 (category, key) が DB に無ければ追加
- 既存があり value が一致 → 何もしない
- 既存があり value が異なる → 旧版を superseded に降格、新版を insert
  + supersedes / superseded_by 双方向リンクを張る
- ペルソナ固有属性 (role/name/personality/gender/first_person/
  speech_style/address_user) には触らない
- episodes / 他 category facts も無傷

ユーザーが運用中に蓄積した会話・好み・知識を一切損なわず、
共通の振る舞いルールだけを最新に保つ。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.boot.defaults import DEFAULT_BOOT_FACTS, DEPRECATED_BOOT_FACTS
from scripts.db.connection import connect
from scripts.shared.embedding import pack
from scripts.shared.ollama import OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")


def upgrade(db_path: Path) -> dict[str, int]:
    """戻り値: {'inserted', 'updated', 'unchanged', 'deprecated'} のカウント."""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0, "deprecated": 0}
    conn = connect(db_path)
    client = OllamaClient()
    try:
        # 1. 廃止された default を active → superseded に降格
        for category, key in DEPRECATED_BOOT_FACTS:
            row = conn.execute(
                "SELECT id FROM facts "
                "WHERE category=? AND key=? AND status='active'",
                (category, key),
            ).fetchone()
            if row is None:
                continue
            conn.execute(
                "UPDATE facts SET status='superseded', "
                "  updated_at=datetime('now', '+9 hours') "
                "WHERE id=?",
                (row[0],),
            )
            conn.commit()
            counts["deprecated"] += 1
            print(f"  deprecated [{category}/{key}]")
        # 2. 現行 default を idempotent に refresh
        for category, key, value, importance in DEFAULT_BOOT_FACTS:
            row = conn.execute(
                "SELECT id, value, importance FROM facts "
                "WHERE category=? AND key=? AND status='active'",
                (category, key),
            ).fetchone()

            if row and row[1] == value and row[2] == importance:
                counts["unchanged"] += 1
                continue

            old_id = row[0] if row else None

            if old_id is not None:
                conn.execute(
                    "UPDATE facts SET status='superseded', "
                    "  updated_at=datetime('now', '+9 hours') "
                    "WHERE id=?",
                    (old_id,),
                )

            cur = conn.execute(
                "INSERT INTO facts(category, key, value, importance, source, supersedes) "
                "VALUES (?, ?, ?, ?, 'upgrade', ?)",
                (category, key, value, importance, old_id),
            )
            new_id = cur.lastrowid

            if old_id is not None:
                conn.execute(
                    "UPDATE facts SET superseded_by=? WHERE id=?",
                    (new_id, old_id),
                )
                counts["updated"] += 1
            else:
                counts["inserted"] += 1

            try:
                vec = client.embed(EMBED_MODEL, f"{category}/{key}: {value}")
                if vec:
                    conn.execute(
                        "INSERT OR REPLACE INTO fact_embeddings(fact_id, embedding) "
                        "VALUES (?, ?)",
                        (new_id, pack(vec)),
                    )
            except Exception as e:
                print(
                    f"  WARN [{category}/{key}] embedded skipped: {e}",
                    file=sys.stderr,
                )

            conn.commit()
            label = "inserted" if old_id is None else "updated"
            print(f"  {label} [{category}/{key}]")

    finally:
        conn.close()
    return counts


def main() -> None:
    p = argparse.ArgumentParser(
        description="Refresh default boot facts (non-destructive)."
    )
    p.add_argument(
        "--db", type=Path, default=None,
        help="DB ファイルパス (未指定時は PERSONA_MEMORY_DB env を使う)",
    )
    args = p.parse_args()

    db_path = args.db
    if db_path is None:
        env_db = os.environ.get("PERSONA_MEMORY_DB", "").strip()
        if not env_db:
            print(
                "ERROR: --db か PERSONA_MEMORY_DB env のどちらかが必要です",
                file=sys.stderr,
            )
            sys.exit(2)
        db_path = Path(env_db)

    if not db_path.exists():
        print(f"ERROR: DB が存在しません: {db_path}", file=sys.stderr)
        sys.exit(2)

    counts = upgrade(db_path)
    print()
    print(
        f"upgrade summary: inserted={counts['inserted']}, "
        f"updated={counts['updated']}, unchanged={counts['unchanged']}, "
        f"deprecated={counts['deprecated']}"
    )


if __name__ == "__main__":
    main()
