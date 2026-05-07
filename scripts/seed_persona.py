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

from scripts.db.connection import connect
from scripts.shared.embedding import pack
from scripts.shared.ollama import OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")


def _upsert_persona_fact(conn, key: str, value: str, importance: int) -> int:
    """category='persona' で upsert (UNIQUE(category,key) WHERE active を使う)。"""
    row = conn.execute(
        "SELECT id FROM facts WHERE category='persona' AND key=? AND status='active'",
        (key,),
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
        "VALUES ('persona', ?, ?, ?, 'init')",
        (key, value, importance),
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
    facts: list[tuple[str, str, int]] = [
        ("role", role, 9),
        ("identity", f"このペルソナの名前は『{name}』", 9),
        ("personality", personality, 9),
        ("gender", f"性別: {gender}", 8),
        ("first_person", f"一人称は『{first_person}』", 8),
        ("speech_style", speech_style, 8),
        ("address_user", address_user, 8),
        (
            "response_brevity",
            "応答は端的に。質問に対しては核だけ即答する。"
            "前置き・状況再確認・『ご質問の件ですが』等の枕詞を省く。"
            "長文は禁止、必要なら 1-2 行の補足のみ。"
            "複数案を並べるのは明示的に求められた時だけ。"
            "理由: 長い応答は読む手間とトークン課金を増やす。",
            9,
        ),
        (
            "confirmation_before_acting",
            "ユーザーが疑問形 (『〜してみる？』『どうする？』『〜できる？』等) "
            "で問いかけた場合、それは提案であって指示ではない。"
            "ユーザーの明示的な承認 (『はい』『お願い』『進めて』『やって』等) "
            "を待ってから実行する。承認なしに勝手に始めない。"
            "推奨や対案を提示した後も同じ — ユーザーの選択を待つ。"
            "例外: typo 修正のような自明な瑣末な作業、"
            "同セッションで既に承認済みの繰り返し作業。"
            "理由: 勝手に始めると時間・計算コストが無駄になり、"
            "ユーザーの意図と逸れる。",
            9,
        ),
    ]

    conn = connect(db_path)
    client = OllamaClient()
    try:
        for key, value, importance in facts:
            fid = _upsert_persona_fact(conn, key, value, importance)
            conn.commit()
            try:
                vec = client.embed(EMBED_MODEL, f"persona/{key}: {value}")
                if vec:
                    conn.execute(
                        "INSERT OR REPLACE INTO fact_embeddings(fact_id, embedding) "
                        "VALUES (?, ?)",
                        (fid, pack(vec)),
                    )
                    conn.commit()
                    print(f"  seeded [persona/{key}] (importance={importance})")
                else:
                    print(
                        f"  WARN seed [persona/{key}] saved without embedding "
                        f"(empty vector)",
                        file=sys.stderr,
                    )
            except Exception as e:
                print(
                    f"  WARN seed [persona/{key}] saved without embedding: {e}",
                    file=sys.stderr,
                )
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Seed initial persona facts.")
    p.add_argument("--db", required=True, type=Path, help="DB ファイルパス")
    p.add_argument("--role", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--gender", required=True)
    p.add_argument("--personality", required=True)
    p.add_argument("--first-person", required=True)
    p.add_argument("--speech-style", required=True)
    p.add_argument("--address-user", required=True)
    args = p.parse_args()

    seed(
        args.db,
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
