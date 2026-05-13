"""ベンチ評価: keyword hit + token reduction の自動集計.

入力: 1 つの scenario に対する **複数 adapter の per_probe.jsonl** 群.
出力: summary.json (KPI 集計) + console 表示.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

import yaml


def load_probes(scenario_dir: Path) -> dict[str, dict]:
    """probes.yaml をロードして id→probe の dict に."""
    with open(scenario_dir / "probes.yaml") as f:
        raw = yaml.safe_load(f)
    return {p["id"]: p for p in raw}


def load_results(report_dir: Path) -> list[dict]:
    """per_probe.jsonl を読む."""
    out: list[dict] = []
    with open(report_dir / "per_probe.jsonl") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def keyword_hit(answer: str, expected_keywords: list[str]) -> list[str]:
    """answer に含まれた expected_keywords を返す. 1 つでも含めば hit 扱い.

    マッチは大文字小文字を区別する (固有名詞前提). 正規化は最小限.
    """
    matched: list[str] = []
    for kw in expected_keywords or []:
        if kw and kw in answer:
            matched.append(kw)
    return matched


def evaluate_run(
    scenario_dir: Path, report_dir: Path, baseline_avg_tokens: float | None,
) -> dict:
    """1 adapter の 1 run を集計して summary dict を返す."""
    probes = load_probes(scenario_dir)
    results = load_results(report_dir)

    auto_total = 0
    auto_hit = 0
    manual_total = 0
    token_samples: list[int] = []

    for r in results:
        probe = probes.get(r["probe_id"])
        if not probe:
            continue
        token_samples.append(r.get("injected_tokens", 0))
        if probe.get("manual_review"):
            manual_total += 1
            continue
        auto_total += 1
        matched = keyword_hit(r.get("answer", ""), probe.get("expected_keywords") or [])
        r["matched_keywords"] = matched
        if matched:
            auto_hit += 1

    avg_tokens = mean(token_samples) if token_samples else 0.0
    p95_tokens = (
        sorted(token_samples)[int(len(token_samples) * 0.95)]
        if len(token_samples) >= 20 else max(token_samples or [0])
    )
    summary = {
        "scenario": scenario_dir.name,
        "report_dir": str(report_dir),
        "total_probes": len(results),
        "auto_probes": auto_total,
        "auto_hit": auto_hit,
        "auto_hit_rate": (auto_hit / auto_total) if auto_total else None,
        "manual_review_pending": manual_total,
        "avg_injected_tokens": avg_tokens,
        "p95_injected_tokens": p95_tokens,
    }
    if baseline_avg_tokens and baseline_avg_tokens > 0:
        summary["baseline_avg_injected_tokens"] = baseline_avg_tokens
        summary["token_reduction_pct"] = (
            1.0 - (avg_tokens / baseline_avg_tokens)
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate bench reports (auto-only; manual probes are flagged)"
    )
    parser.add_argument("--scenario", type=str, required=True,
                        help="e.g. 01-renju-dev")
    parser.add_argument("--report-root", type=Path,
                        default=Path("bench/reports"))
    parser.add_argument("--scenario-root", type=Path,
                        default=Path("bench/scenarios"))
    parser.add_argument("--baseline-report", type=Path, default=None,
                        help="baseline (no_memory) の per_probe.jsonl があるディレクトリ")
    args = parser.parse_args()

    scenario_dir = args.scenario_root / args.scenario
    if not scenario_dir.exists():
        raise SystemExit(f"scenario not found: {scenario_dir}")

    baseline_avg: float | None = None
    if args.baseline_report:
        base = load_results(args.baseline_report)
        if base:
            baseline_avg = mean(r.get("injected_tokens", 0) for r in base)

    for report_dir in sorted((args.report_root).glob(f"*_{args.scenario}_*")):
        summary = evaluate_run(scenario_dir, report_dir, baseline_avg)
        out_path = report_dir / "summary.json"
        out_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2)
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
