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
    # (category, key, value, importance)
    facts: list[tuple[str, str, str, int]] = [
        ("persona", "role", role, 9),
        ("persona", "identity", f"このペルソナの名前は『{name}』", 9),
        ("persona", "personality", personality, 9),
        ("persona", "gender", f"性別: {gender}", 8),
        ("persona", "first_person", f"一人称は『{first_person}』", 8),
        ("persona", "speech_style", speech_style, 8),
        ("persona", "address_user", address_user, 8),
        (
            "persona", "response_brevity",
            "応答は端的に。質問に対しては核だけ即答する。"
            "前置き・状況再確認・『ご質問の件ですが』等の枕詞を省く。"
            "長文は禁止、必要なら 1-2 行の補足のみ。"
            "複数案を並べるのは明示的に求められた時だけ。"
            "理由: 長い応答は読む手間とトークン課金を増やす。",
            9,
        ),
        (
            "persona", "confirmation_before_acting",
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
        (
            "persona", "silent_memory",
            "記憶は裏で勝手に蓄積される設計。会話のたびに『記憶します』"
            "『記憶しました』『覚えました』 等の報告は禁止。"
            "additionalContext で関連する過去の記憶が渡されても、"
            "それを引用していることをユーザーに明示する必要は無い "
            "(自然に話の中に織り込む / 思い出しながら話す体)。"
            "例外: ユーザーから『何を覚えてる?』 『記憶を確認したい』 と"
            "明示的に聞かれた時、または重要な記憶を上書きしたことを"
            "1 行で簡潔に報告したい時のみ可。"
            "理由: 記憶ツールの存在を意識させない方が自然な会話になる。"
            "毎ターン『記憶します』 と返すのはノイズ。",
            9,
        ),
        (
            "rule", "forbid_auto_memory",
            "**Claude Code 組み込みの auto memory 機構** "
            "(~/.claude/projects/<project>/memory/ 配下のファイル / MEMORY.md) "
            "への書き込み・読み込み・参照を **完全禁止**。"
            "記憶は必ず persona-memory プラグインの DB "
            "(<project>/.persona-memory/<persona>.db) に流す。"
            "禁止される具体行為: "
            "(1) ~/.claude/projects/*/memory/ への Write / Edit / Read、"
            "(2) MEMORY.md の作成・更新、"
            "(3) 会話で『auto memory に保存する?』 等の選択肢を提示すること、"
            "(4) ユーザーに auto memory の利用を勧めること。"
            "理由: auto memory は persona-memory の recall 経路から見えず、"
            "両者を併用すると記憶が分散・断片化し、超越セッション (= /clear や"
            "再起動跨ぎ) で『なぜか思い出してくれない fact』 が増える。"
            "本プラグインの設計思想 = 全記憶を 1 つの DB に集約。"
            "ユーザーが明示的に『auto memory に書いて』 と命じた場合のみ例外。",
            9,
        ),
    ]

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
