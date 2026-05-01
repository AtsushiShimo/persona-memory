#!/usr/bin/env python3
"""Seed initial persona facts into a freshly initialized persona-memory DB.

Called by scripts/init.sh after setup.sh has created the venv + DB.
Uses the same embedding pipeline as the MCP server (Ollama) so seed facts
participate in vector search from the start.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import db, embedding  # noqa: E402


async def seed(
    *,
    role: str,
    name: str,
    gender: str,
    personality: str,
    first_person: str,
    speech_style: str,
    address_user: str,
) -> None:
    # Ordered by importance to match the design priority ("役割が一番大事").
    facts: list[tuple[str, str, str, int]] = [
        ("persona", "role", role, 9),
        ("persona", "identity", f"このペルソナの名前は『{name}』", 9),
        ("persona", "personality", personality, 9),
        ("persona", "gender", f"性別: {gender}", 8),
        ("persona", "first_person", f"一人称は『{first_person}』", 8),
        ("persona", "speech_style", speech_style, 8),
        ("persona", "address_user", address_user, 8),
    ]

    for cat, key, val, imp in facts:
        fid = db.upsert_fact(
            category=cat, key=key, value=val, importance=imp, source="init"
        )
        try:
            vec = await embedding.embed_text(f"{cat}/{key}: {val}")
            db.write_fact_embedding(fid, embedding.pack_embedding(vec))
            print(f"  seeded [{cat}/{key}] (importance={imp})")
        except embedding.EmbeddingError as e:
            print(
                f"  WARN seed [{cat}/{key}] saved without embedding: {e}",
                file=sys.stderr,
            )


def main() -> None:
    p = argparse.ArgumentParser(description="Seed initial persona facts.")
    p.add_argument("--role", required=True, help="役割・立場 (最重要)")
    p.add_argument("--name", required=True)
    p.add_argument("--gender", required=True)
    p.add_argument("--personality", required=True)
    p.add_argument("--first-person", required=True, help="一人称 (僕/俺/私/我輩 等)")
    p.add_argument(
        "--speech-style",
        required=True,
        help="口調・話し方 (例: 敬語 / タメ口 / 古風 / フランク 等)",
    )
    p.add_argument(
        "--address-user",
        required=True,
        help="ユーザーの呼び方 (君/お前/あなた/様付け 等)",
    )
    args = p.parse_args()
    asyncio.run(
        seed(
            role=args.role,
            name=args.name,
            gender=args.gender,
            personality=args.personality,
            first_person=args.first_person,
            speech_style=args.speech_style,
            address_user=args.address_user,
        )
    )


if __name__ == "__main__":
    main()
