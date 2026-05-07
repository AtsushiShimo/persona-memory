#!/usr/bin/env python3
"""新スキーマ向けに persona facts を seed する。

scripts/init.sh から呼ばれ、setup.sh が venv + DB を作った後に実行される。
新モジュール (scripts.db / scripts.shared.ollama / scripts.shared.embedding) を
使って seed する。Ollama 未起動時は embedding なしで保存 (recall は弱まるが
SessionStart の boot 層注入には影響しない)。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.boot.defaults import DEFAULT_BOOT_FACTS
from scripts.db.connection import connect
from scripts.shared.embedding import pack
from scripts.shared.ollama import OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")


def _upsert_boot_fact(conn, category: str, key: str, value: str, importance: int) -> int:
    """boot 層 (persona / rule) に upsert (UNIQUE(category,key) WHERE active を使う)。"""
    row = conn.execute(
        "SELECT id FROM facts WHERE category=? AND key=? AND status='active'",
        (category, key),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE facts SET value=?, importance=?, "
            "  updated_at=datetime('now', '+9 hours') "
            "WHERE id=?",
            (value, importance, row[0]),
        )
        return row[0]
    cur = conn.execute(
        "INSERT INTO facts(category, key, value, importance, source) "
        "VALUES (?, ?, ?, ?, 'init')",
        (category, key, value, importance),
    )
    return cur.lastrowid


def seed(
    db_path: Path,
    *,
    role: str,
    name: str,
    gender: str,
    personality: str,
    first_person: str,
    speech_style: str,
    address_user: str,
) -> None:
    # ペルソナ固有属性 (init の質問回答から組み立てる) +
    # 共通 default 行動指針 (scripts/boot/defaults.py から import)
    user_facts: list[tuple[str, str, str, int]] = [
        ("persona", "role", role, 9),
        ("persona", "identity", f"このペルソナの名前は『{name}』", 9),
        ("persona", "personality", personality, 9),
        ("persona", "gender", f"性別: {gender}", 8),
        ("persona", "first_person", f"一人称は『{first_person}』", 8),
        ("persona", "speech_style", speech_style, 8),
        ("persona", "address_user", address_user, 8),
    ]
    facts = user_facts + DEFAULT_BOOT_FACTS

    conn = connect(db_path)
    client = OllamaClient()
    try:
        for category, key, value, importance in facts:
            fid = _upsert_boot_fact(conn, category, key, value, importance)
            conn.commit()
            try:
                vec = client.embed(EMBED_MODEL, f"{category}/{key}: {value}")
                if vec:
                    conn.execute(
                        "INSERT OR REPLACE INTO fact_embeddings(fact_id, embedding) "
                        "VALUES (?, ?)",
                        (fid, pack(vec)),
                    )
                    conn.commit()
                    print(f"  seeded [{category}/{key}] (importance={importance})")
                else:
                    print(
                        f"  WARN seed [{category}/{key}] saved without embedding "
                        f"(empty vector)",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(
                    f"  WARN seed [{category}/{key}] saved without embedding: {e}",
                    file=sys.stderr,
                )
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Seed initial persona facts.")
    p.add_argument("--db", type=Path, default=None,
                   help="DB ファイルパス (未指定時は PERSONA_MEMORY_DB env を使う)")
    p.add_argument("--role", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--gender", required=True)
    p.add_argument("--personality", required=True)
    p.add_argument("--first-person", required=True)
    p.add_argument("--speech-style", required=True)
    p.add_argument("--address-user", required=True)
    args = p.parse_args()

    db_path = args.db
    if db_path is None:
        env_db = os.environ.get("PERSONA_MEMORY_DB", "").strip()
        if not env_db:
            print("ERROR: --db か PERSONA_MEMORY_DB env のどちらかが必要です", file=sys.stderr)
            sys.exit(2)
        db_path = Path(env_db)

    seed(
        db_path,
        role=args.role,
        name=args.name,
        gender=args.gender,
        personality=args.personality,
        first_person=args.first_person,
        speech_style=args.speech_style,
        address_user=args.address_user,
    )


if __name__ == "__main__":
    main()
