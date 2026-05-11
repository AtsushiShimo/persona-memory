#!/usr/bin/env python3
"""既存ペルソナ DB に persona/stance を追加する (未設定の場合のみ).

設計判断 (0.5.21):
- 既に動いているペルソナ (ソフィア・凜等) は init 時 stance を持っていない
  ため、 後から追加する経路を用意する.
- **後からの変更は対象外** (= 既に stance がある DB は触らない). 変更機能は
  別途検討する.
- boot 層 dirty フラグを立てて、 次の UserPromptSubmit で即時再注入される
  ようにする (= Claude Code の再起動不要で性格反映).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.boot.inject import mark_dirty
from scripts.db.connection import connect
from scripts.seed_persona import (
    _parse_stance_csv,
    _stance_to_natural_language,
    _upsert_boot_fact,
)
from scripts.shared.embedding import pack
from scripts.shared.ollama import OllamaClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")


def add_stance_if_missing(db_path: Path, stance: list[int]) -> str:
    """persona/stance が未設定なら追加. 既存なら "exists" 返却.

    戻り値: "added" / "exists"
    """
    conn = connect(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM facts "
            "WHERE category='persona' AND key='stance' AND status='active'"
        ).fetchone()
        if existing:
            return "exists"
        value = _stance_to_natural_language(stance)
        fid = _upsert_boot_fact(conn, "persona", "stance", value, 9)
        conn.commit()
        # embedding 化 (Ollama 落ちでも追加は完了する設計, fail-open)
        client = OllamaClient()
        try:
            vec = client.embed(EMBED_MODEL, f"persona/stance: {value}")
            if vec:
                conn.execute(
                    "INSERT OR REPLACE INTO fact_embeddings"
                    "(fact_id, embedding) VALUES (?, ?)",
                    (fid, pack(vec)),
                )
                conn.commit()
        except Exception as e:
            print(
                f"  WARN persona/stance saved without embedding: {e}",
                file=sys.stderr,
            )
        # boot 層 dirty マーク: 次の UserPromptSubmit で即時再注入させる
        mark_dirty(conn)
        return "added"
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="既存ペルソナ DB に persona/stance を追加 (未設定の場合のみ)"
    )
    p.add_argument("--db", required=True, type=Path,
                   help="ペルソナ DB ファイルパス")
    p.add_argument("--stance", required=True,
                   help='5 軸 CSV "v1,v2,v3,v4,v5" (各 -2..+2). '
                        '軸順: 保守-革新 / 楽観-悲観 / 直感-分析 / 慎重-大胆 / 共感-論理')
    args = p.parse_args()

    if not args.db.exists():
        print(f"ERROR: DB が存在しません: {args.db}", file=sys.stderr)
        return 2

    try:
        values = _parse_stance_csv(args.stance)
    except ValueError as e:
        print(f"ERROR: --stance: {e}", file=sys.stderr)
        return 2

    result = add_stance_if_missing(args.db, values)
    if result == "exists":
        print("既に persona/stance が設定されています. 何もしませんでした.")
        print("(後からの変更機能は別途検討)")
        return 0
    print(f"persona/stance を追加しました (stance={args.stance})")
    print("次のユーザー発話の SessionStart 相当で性格に反映されます.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
