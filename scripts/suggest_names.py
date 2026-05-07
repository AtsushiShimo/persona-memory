#!/usr/bin/env python3
"""ペルソナ名 3 案を生成。

優先順位: Claude (`claude -p`) → heavy Ollama (gemma3:12b) → light Ollama (gemma3:4b)
init は 1 回しか走らないので品質重視で Claude を最優先にする。

stdlib only — init.sh が venv を作る *前* に走るため。

出力: 1 行 1 名前で stdout に並べる。失敗時は空 stdout で exit 0
(呼び出し側 init.sh は自由入力にフォールバック)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
LIGHT_MODEL = os.environ.get("PERSONA_LIGHT_MODEL", os.environ.get("PERSONA_JUDGE_MODEL", "gemma3:4b"))
HEAVY_MODEL = os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b")
TIMEOUT = float(os.environ.get("PERSONA_NAME_SUGGEST_TIMEOUT", "60.0"))


def build_prompt(
    role: str,
    gender: str,
    personality: str,
    first_person: str,
    speech_style: str,
) -> str:
    return (
        "あなたはアニメ・漫画・ライトノベルのキャラクター命名を専門とするクリエイターです。\n"
        "次の設定を持つペルソナに、世界観と性格を象徴する **印象的なファーストネーム** を 3 つ提案してください。\n\n"
        f"## 設定\n"
        f"- 役割: {role}\n"
        f"- 性別: {gender}\n"
        f"- 性格: {personality}\n"
        f"- 一人称: {first_person}\n"
        f"- 口調: {speech_style}\n\n"
        "## 命名指針\n"
        "- 役割の本質を象徴するモチーフを選ぶ:\n"
        "  - 知識・分析・調査系 → 神話の知恵の女神 (Athena, Sophia, Saraswati, Minerva), "
        "賢者・占星術師・古代魔導士の名 (Solomon, Merlin, Cassandra), "
        "知恵を意味する語 (慧, 叡, 智, 玲)\n"
        "  - 戦闘・批評系 → 鋭利・刃物・雷霆の象徴 (Fenris, Loki, 紫雷, 朔)\n"
        "  - 教育・支援系 → 守護・癒し・光の象徴 (Lumen, Iris, Vesta, 灯, 暁)\n"
        "  - 議論・壁打ち系 → 弁証・反響・鏡の象徴 (Echo, Mira, 響, 詠)\n"
        "  - 創作・記録系 → 言葉・物語の象徴 (Lyra, Calliope, 詩, 言)\n"
        "- **厨二感 / 神秘感 / 中性的な美しさ OK**。むしろ歓迎\n"
        "- 性格と一致する音 (冷静沈着 → クールで硬質な響き、明るく前向き → 弾むような響き)\n"
        "- 性別が女性なら女性的な響き、男性なら男性的、中性ならどちらでも可\n"
        "- ファーストネームのみ (苗字 / 二つ名は不要)\n"
        "- 漢字・ひらがな・カタカナ・ローマ字 自由\n"
        "- 3 つは互いに **被らない方向性** (例: 1 つは神話系、1 つは漢字熟語、1 つはカタカナ)\n\n"
        "## 出力形式\n"
        '純粋な JSON のみ。説明・前置き・コードフェンス禁止: {"names": ["名前1", "名前2", "名前3"]}\n'
    )


def _parse_names(raw: str) -> list[str]:
    if not raw:
        return []
    # コードフェンス除去
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    # 配列または {"names": [...]} のどちらでも対応
    m = re.search(r"\{.*\}|\[.*\]", cleaned, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []

    candidates: list[str] = []
    if isinstance(data, list):
        candidates = [str(x).strip() for x in data]
    elif isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                candidates = [str(x).strip() for x in v]
                break
    return [c for c in candidates if c][:3]


def suggest_via_claude(prompt: str) -> list[str]:
    """`claude -p` 子プロセス。失敗時は空リスト。"""
    env = os.environ.copy()
    env["PERSONA_ESCALATION_CHILD"] = "1"
    env.setdefault("PERSONA_WRITE_DISABLE", "1")
    env.setdefault("PERSONA_RECALL_DISABLE", "1")
    try:
        r = subprocess.run(
            ["claude", "-p", prompt],
            env=env, capture_output=True, timeout=TIMEOUT, text=True,
        )
        if r.returncode != 0:
            return []
        return _parse_names(r.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    except Exception:
        return []


def suggest_via_ollama(prompt: str, model: str) -> list[str]:
    body = json.dumps(
        {"model": model, "prompt": prompt, "stream": False, "format": "json"}
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
    return _parse_names((payload.get("response") or "").strip())


def suggest(
    role: str,
    gender: str,
    personality: str,
    first_person: str,
    speech_style: str,
) -> list[str]:
    """Claude → heavy → light の順に試し、最初に 3 件揃った結果を返す。"""
    prompt = build_prompt(role, gender, personality, first_person, speech_style)

    for fn, label in [
        (lambda: suggest_via_claude(prompt), "claude"),
        (lambda: suggest_via_ollama(prompt, HEAVY_MODEL), f"ollama:{HEAVY_MODEL}"),
        (lambda: suggest_via_ollama(prompt, LIGHT_MODEL), f"ollama:{LIGHT_MODEL}"),
    ]:
        names = fn()
        if len(names) >= 3:
            sys.stderr.write(f"[suggest_names] used {label}\n")
            return names
        elif names:
            sys.stderr.write(f"[suggest_names] {label} returned only {len(names)} name(s), trying next\n")

    return []


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
