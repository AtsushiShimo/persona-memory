"""recall デバッグログ (§7).

PERSONA_RECALL_LOG_LEVEL = ""/"0"/"false"/"off"  → 完全 OFF
                        = "1"/"true"/"on"/"c"  → level c (最詳細、default)
                        = "a"                  → keywords のみ
                        = "b"                  → keywords + hits

出力先: stderr + ログファイル (data/debug-recall.log) 併用。
**additionalContext には入れない** (Claude 本体への注入文を汚さない).

0.8.9 rename: 旧名 PERSONA_MEMORY_DEBUG は同名で debug mode の責務 A (DB
block skip) を 0.8.8 まで持っていた. 名前空間衝突を解消するため本 module の
責務 B (= recall ログレベル) を PERSONA_RECALL_LOG_LEVEL に rename. 旧名は
読まない (= 後方互換無し, design_drift_zero_tolerance).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# level の集合 (a < b < c の包含関係)
_LEVEL_RANK = {"a": 1, "b": 2, "c": 3}


def _resolve_level() -> int:
    raw = os.environ.get("PERSONA_RECALL_LOG_LEVEL", "").strip().lower()
    if raw in ("", "0", "false", "off"):
        return 0
    if raw in ("1", "true", "on"):
        return _LEVEL_RANK["c"]  # default = 最詳細
    return _LEVEL_RANK.get(raw, _LEVEL_RANK["c"])


def is_enabled() -> bool:
    return _resolve_level() > 0


def _resolve_log_path() -> Path | None:
    explicit = os.environ.get("PERSONA_DEBUG_LOG_PATH", "").strip()
    if explicit:
        return Path(explicit)
    db = os.environ.get("PERSONA_MEMORY_DB", "").strip()
    if db:
        # data/<persona>.db と同階層に置く
        return Path(db).parent / "debug-recall.log"
    return None


def _emit(stage: str, level_required: str, payload: dict) -> None:
    if _resolve_level() < _LEVEL_RANK[level_required]:
        return
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}][persona-memory:{stage}] " + json.dumps(payload, ensure_ascii=False)

    sys.stderr.write(line + "\n")

    p = _resolve_log_path()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # ログ失敗で本処理を止めない


# ── stage-specific ──────────────────────────────────────────────────────────

def log_extract_prompt(prompt: str) -> None:
    """level c: ローカル LLM への入力プロンプト全文"""
    _emit("recall.extract.prompt", "c", {
        "prompt": prompt,
        "length": len(prompt),
    })


def log_extract_response(response: str) -> None:
    """level c: ローカル LLM の生レスポンス"""
    _emit("recall.extract.response", "c", {
        "response": response,
        "length": len(response),
    })


def log_keywords(content: str, buffer: list[dict], keywords: list[str]) -> None:
    """level a: パース済みキーワードと意図"""
    _emit("recall.keywords", "a", {
        "content": content[:200],
        "buffer_n": len(buffer),
        "keywords": keywords,
    })


def log_hits(keyword_count: int, hits: list[dict]) -> None:
    """level b: DB 検索ヒット (距離・importance 含む)"""
    _emit("recall.hits", "b", {
        "keywords": keyword_count,
        "hits": hits,
    })


def log_final_prompt(additional_context: str) -> None:
    """level c: メインに渡す最終 additionalContext"""
    _emit("recall.final", "c", {
        "additional_context": additional_context,
        "length": len(additional_context),
    })
