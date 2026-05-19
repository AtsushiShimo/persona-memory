"""lint LLM (judge_conflict) 主エントリ (0.8.0 — Cozo 単独経路).

write 完了後の tail として detached child で起動される (= write_tail trigger).
新 fact の近傍のみ対象とする limited lint. 自動解消は confidence >= 90.

judge_conflict (= LLM 呼び出し本体) は DB 非依存で他モジュールから共有される.
SQLite 経路 (= scripts.db / fact_embeddings) は 0.8.0 で完全廃止.
"""
from __future__ import annotations

import json
import os
import re
import sys

from scripts.db_cozo.connection import init_db
from scripts.db_cozo.lint import lint_around_fact_cozo, record_lint_run
from scripts.db_cozo.wire import cozo_db_path_for, cozo_db_present
from scripts.shared.env import get_db_path
from scripts.shared.ollama import LLMClient, OllamaClient

JUDGE_MODEL = os.environ.get(
    "PERSONA_LINT_MODEL",
    os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b"),
)
LINT_NUM_CTX = int(os.environ.get("PERSONA_LINT_NUM_CTX", "16384"))
NEIGHBOR_TOP_K = int(os.environ.get("PERSONA_LINT_NEIGHBOR_TOP_K", "5"))


_PROMPT = """\
2 つの記憶が **同じ事柄について論理的に矛盾している** か判定する。

記憶 A: {a}
記憶 B: {b}

## 判定基準 (厳格)

**矛盾と判定する条件 (両方を満たす時のみ)**:
1. 同じ対象 / 同じ属性について述べている (例: 同一人物の同一の好み、
   同じ物の同じ性質、同じ事実の同じ側面)
2. その属性について **両立しない値** が示されている

**矛盾と判定しない例**:
- 別の対象 / 別の属性
- 同じ対象でも別の側面 (例: コーヒーの焙煎度と砂糖の有無は別属性)
- 一方が一般論、 他方が特定事例

## 出力

JSON のみ (説明・前置き・コードフェンス禁止):
{{"contradict": true|false, "confidence": 0-100}}
"""


def judge_conflict(
    value_a: str, value_b: str, client: LLMClient,
    model: str = JUDGE_MODEL,
) -> tuple[bool, int]:
    """2 つの fact value が論理的に矛盾するか judge LLM で判定.

    戻り値: (contradict, confidence). LLM 失敗時は (False, 0).
    """
    prompt = _PROMPT.format(a=value_a, b=value_b)
    try:
        raw = client.generate(model, prompt, num_ctx=LINT_NUM_CTX)
    except Exception:
        return (False, 0)
    if not raw:
        return (False, 0)
    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE,
    )
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return (False, 0)
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return (False, 0)
    if not isinstance(data, dict):
        return (False, 0)
    contradict = bool(data.get("contradict", False))
    try:
        confidence = max(0, min(100, int(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0
    return (contradict, confidence)


def run(fact_ids: list[int], trigger: str = "manual") -> int:
    db_path = get_db_path()
    if db_path is None or not cozo_db_present(db_path):
        return 0
    if os.environ.get("PERSONA_LINT_DISABLE") == "1":
        return 0
    cozo_path = cozo_db_path_for(db_path)
    llm = OllamaClient()
    total_pairs = 0
    total_flagged = 0
    total_auto = 0
    seen: set = set()
    for fid in fact_ids:
        try:
            client = init_db(cozo_path)
            r = lint_around_fact_cozo(
                client, fid, llm,
                seen_pairs=seen,
                neighbor_top_k=NEIGHBOR_TOP_K,
            )
            total_pairs += r.get("pairs_examined", 0)
            total_flagged += r.get("flagged", 0)
            total_auto += r.get("auto_resolved", 0)
        except Exception as e:
            sys.stderr.write(f"[persona-memory] lint fact={fid} failed: {e}\n")
    try:
        client = init_db(cozo_path)
        record_lint_run(
            client,
            pairs_examined=total_pairs,
            flagged=total_flagged,
            auto_resolved=total_auto,
            trigger=trigger,
        )
    except Exception as e:
        sys.stderr.write(f"[persona-memory] record_lint_run failed: {e}\n")
    return 0


STDIN_WAIT_TIMEOUT = float(os.environ.get("PERSONA_LINT_STDIN_TIMEOUT", "30"))


def main() -> int:
    # 親 (spawn.py) が payload を流す前にハーネスごと死ぬと、 子はここで
    # 永遠に stdin EOF を待ち続けて zombie 化する (= scripts.write.run と同パターン).
    # select で短時間だけ stdin を覗き、 アイドルなら諦める.
    import select
    r, _, _ = select.select([sys.stdin], [], [], STDIN_WAIT_TIMEOUT)
    if not r:
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return 0
    fact_ids = payload.get("fact_ids") or []
    trigger = payload.get("trigger", "manual")
    if not fact_ids:
        return 0
    return run(fact_ids, trigger)


if __name__ == "__main__":
    sys.exit(main())
