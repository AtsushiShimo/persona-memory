#!/usr/bin/env python3
"""Compression hallucination measurement (改善 3).

Drives `scripts/proxy_recall.compress_episodes()` against synthesized
episode lists derived from bench scenarios, and uses gemma3:12b as an
LLM judge (same one used in bench.py) to measure two things:

1. **fact 保存率**: Each scenario's `expected_facts` (defined in
   tests/bench.py) — does the compressed summary preserve them?
2. **ハルシネーション率**: A fixed list of `NONEXISTENT_FACTS` that are
   NOT mentioned in any scenario — does the compressed summary mistakenly
   claim them?

Run:
  python3 tests/test_compression.py                # all scenarios
  python3 tests/test_compression.py --scenario 10  # only scenario 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.proxy_recall import compress_episodes  # noqa: E402
from tests.bench import SCENARIOS, llm_judge  # noqa: E402

# Scenario-agnostic な「存在しない事実」リスト。bench scenarios のどの setup
# にも登場しないトピックを選ぶことで「圧縮 LLM が捏造したか」だけを測れる。
# Neovim は scenario 13 に登場するので Emacs を入れる (区別力テスト)。
# AWS / GCP は scenario 13 に出るので避けた。
NONEXISTENT_FACTS = [
    "ユーザーの趣味は登山である",
    "ユーザーは Lisp が好きである",
    "ユーザーは ImageMagick を業務で日常的に使っている",
    "ユーザーは渋谷オフィスに毎日出社している",
    "ユーザーは PHP を 5 年以上使っている",
    "ユーザーは Emacs を使っている",
]


def setup_to_episodes(setup_text: str) -> list[dict[str, Any]]:
    """bench Scenario.setup を compress_episodes が食う形式に変換。

    setup は user 単独宣言なので raw_user 1 件で十分。created_at は判定に
    影響しないが、形式としては必要なので固定値を入れる。
    """
    return [
        {
            "role": "raw_user",
            "content": setup_text,
            "summary": None,
            "created_at": "2026-05-01 12:00:00",
        }
    ]


def run_one(sc) -> dict[str, Any]:
    """1 scenario について保存率と幻覚率を計測。"""
    episodes = setup_to_episodes(sc.setup)
    summary = compress_episodes(sc.probe, episodes)
    if not summary:
        return {
            "scenario": sc.id,
            "name": sc.name,
            "summary": None,
            "preserve_yes": 0,
            "preserve_total": 0,
            "halluc_yes": 0,
            "halluc_total": 0,
            "preserve_detail": {},
            "halluc_detail": {},
            "error": "compress_episodes returned None",
        }

    p_yes, p_total, p_detail = llm_judge(summary, sc.expected_facts)
    h_yes, h_total, h_detail = llm_judge(summary, NONEXISTENT_FACTS)

    return {
        "scenario": sc.id,
        "name": sc.name,
        "summary": summary,
        "preserve_yes": p_yes,
        "preserve_total": p_total,
        "halluc_yes": h_yes,
        "halluc_total": h_total,
        "preserve_detail": p_detail,
        "halluc_detail": h_detail,
        "error": None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", type=int, default=None)
    ap.add_argument(
        "--show-summary",
        action="store_true",
        help="Print the compressed summary for each scenario",
    )
    args = ap.parse_args()

    scenarios = (
        [s for s in SCENARIOS if s.id == args.scenario]
        if args.scenario
        else SCENARIOS
    )
    # expected_facts が空の scenario は保存率を計れないのでスキップ
    scenarios = [s for s in scenarios if s.expected_facts]
    if not scenarios:
        print("no scenarios with expected_facts to measure", file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = []
    for sc in scenarios:
        print(f"\n========== Scenario {sc.id}: {sc.name} ==========", flush=True)
        r = run_one(sc)
        results.append(r)
        if r["error"]:
            print(f"  [ERROR] {r['error']}")
            continue
        print(f"  preserve = {r['preserve_yes']}/{r['preserve_total']}")
        for fact, v in r["preserve_detail"].items():
            mark = "✓" if v == "yes" else ("?" if v == "unsure" else "✗")
            print(f"    {mark} {fact}")
        print(
            f"  halluc   = {r['halluc_yes']}/{r['halluc_total']} "
            "(lower is better)"
        )
        for fact, v in r["halluc_detail"].items():
            if v == "yes":
                print(f"    [HALLUCINATED] {fact}")
        if args.show_summary:
            print(f"  --- summary ---\n{r['summary']}")

    # Aggregate
    print("\n\n========== SUMMARY ==========")
    p_y = sum(r["preserve_yes"] for r in results)
    p_t = sum(r["preserve_total"] for r in results)
    h_y = sum(r["halluc_yes"] for r in results)
    h_t = sum(r["halluc_total"] for r in results)
    print(
        f"全体保存率  : {p_y}/{p_t} = "
        f"{(p_y / p_t * 100) if p_t else 0:.1f}%"
    )
    print(
        f"全体幻覚率  : {h_y}/{h_t} = "
        f"{(h_y / h_t * 100) if h_t else 0:.1f}% (lower is better)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
