#!/usr/bin/env python3
"""Ask the local Ollama judge model to suggest 3 persona names that fit the
already-chosen role/gender/personality/voice settings.

Outputs the suggestions one per line on stdout. On any failure (Ollama
unreachable, malformed response, etc.), exits 0 with empty output so the
caller can fall back to a plain free-form prompt.

Stdlib only — runs under system python3, since this is invoked during
init.sh BEFORE setup.sh has built the venv.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
JUDGE_MODEL = os.environ.get("PERSONA_JUDGE_MODEL", "gemma3:4b")
TIMEOUT = float(os.environ.get("PERSONA_NAME_SUGGEST_TIMEOUT", "30.0"))


def suggest(
    role: str,
    gender: str,
    personality: str,
    first_person: str,
    speech_style: str,
) -> list[str]:
    prompt = (
        "次の設定に合うキャラクターの名前を 3 つ提案してください。\n\n"
        f"役割: {role}\n"
        f"性別: {gender}\n"
        f"性格: {personality}\n"
        f"一人称: {first_person}\n"
        f"口調: {speech_style}\n\n"
        "条件:\n"
        "- ファーストネームのみ (苗字なし)\n"
        "- 役割と性格と響きが合うもの\n"
        "- ひらがな・カタカナ・漢字いずれも可。被らないバリエーション\n"
        '- 出力は JSON のみ: {"names": ["名前1", "名前2", "名前3"]}\n'
    )
    body = json.dumps(
        {
            "model": JUDGE_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []
    raw = (payload.get("response") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    candidates: list[str] = []
    if isinstance(data, list):
        candidates = [str(x).strip() for x in data]
    elif isinstance(data, dict):
        # Look for any list value (handles {"names": [...]} and quirky variants).
        for v in data.values():
            if isinstance(v, list):
                candidates = [str(x).strip() for x in v]
                break
    return [c for c in candidates if c][:3]


def main() -> None:
    p = argparse.ArgumentParser(description="Suggest 3 persona names.")
    p.add_argument("--role", required=True)
    p.add_argument("--gender", required=True)
    p.add_argument("--personality", required=True)
    p.add_argument("--first-person", required=True)
    p.add_argument("--speech-style", required=True)
    args = p.parse_args()
    for name in suggest(
        role=args.role,
        gender=args.gender,
        personality=args.personality,
        first_person=args.first_person,
        speech_style=args.speech_style,
    ):
        print(name)


if __name__ == "__main__":
    main()
