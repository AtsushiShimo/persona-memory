#!/usr/bin/env python3
"""UserPromptSubmit hook: dynamic memory recall via local embedding + sqlite-vec.

Reads JSON from stdin (Claude Code hook payload), embeds the user prompt
locally via Ollama, and emits additionalContext JSON to stdout for Claude
Code to splice into the prompt.

Recall は以下の 3 段で組み立てる:

1. **ベクトル候補取得**: sqlite-vec から TOP_K * 4 件を距離順に粗く拾う。
2. **importance-aware recency decay**: `effective_distance = raw +
   λ * log(1 + age_d/365) * (1 - importance/10)`。importance が高い fact ほど
   age penalty が緩み、低 importance の古い fact ほど沈む。
3. **category 別 window のカスケード**: category ごとに「直近 X 日 → ヒット
   不足なら拡大」というステージ列を持つ。`profile` / `rule` は無期限、
   `context` は短命、`preference` / `skill` は中期。fact 自体は削除しない。

Designed to fail open: any error -> exit 0 with no output, so the user's
prompt passes through unchanged.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import struct
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# DB は Asia/Tokyo (JST = UTC+9) で timestamp を保存する規約。
# proxy_recall の age 計算もこの規約に合わせ、JST naive 比較で行う。
JST = timezone(timedelta(hours=9))
from pathlib import Path

try:
    import httpx
    import sqlite_vec
except Exception:
    sys.exit(0)

# ── Secret detection ──────────────────────────────────────────────────────────

_SECRET_PATTERNS = re.compile(
    r"(?:"
    r"sk-proj-[A-Za-z0-9\-_]{20,}"       # OpenAI project key
    r"|sk-[A-Za-z0-9\-_]{20,}"           # OpenAI / Anthropic API key
    r"|ghp_[A-Za-z0-9]{36,}"             # GitHub PAT
    r"|gh[ousr]_[A-Za-z0-9]{36,}"        # GitHub OAuth/User/Server/Refresh
    r"|AKIA[A-Z0-9]{16}"                 # AWS Access Key ID
    r"|sk_live_[A-Za-z0-9]+"             # Stripe live
    r"|sk_test_[A-Za-z0-9]+"             # Stripe test
    r"|rk_live_[A-Za-z0-9]+"             # Stripe restricted live
    r"|xoxb-[A-Za-z0-9\-]+"             # Slack bot token
    r"|xoxp-[A-Za-z0-9\-]+"             # Slack user token
    r"|ya29\.[A-Za-z0-9\-_]+"            # Google OAuth access token
    r"|AIza[A-Za-z0-9\-_]{35}"           # Google API key
    r"|eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+"  # JWT
    r")"
)


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    return -sum((f / len(s)) * math.log2(f / len(s)) for f in freq.values())


def detect_secrets(text: str) -> list[str]:
    """Return masked previews of detected secrets. Empty list = clean."""
    found: list[str] = []

    # 1. Known-prefix patterns
    for m in _SECRET_PATTERNS.finditer(text):
        val = m.group()
        found.append(val[:6] + "..." + val[-4:])

    # 2. High-entropy ASCII tokens ≥ 32 chars (passwords, random tokens)
    #    Only flag pure ASCII alpha-num strings to avoid false positives on URLs.
    if not found:
        for token in re.findall(r"[A-Za-z0-9+/=_\-]{32,}", text):
            if _shannon_entropy(token) > 4.2:
                found.append(token[:6] + "..." + token[-4:])

    return found


def store_to_keychain(label: str, value: str) -> bool:
    """Store a secret value in macOS Keychain under service 'persona-memory'."""
    try:
        result = subprocess.run(
            ["security", "add-generic-password",
             "-s", "persona-memory", "-a", label, "-w", value, "-U"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
DB_PATH = os.environ.get("PERSONA_MEMORY_DB", "")
# 想起方針 (rule/recall_priority): 優先順位は (1) 正しい記憶 > (2) Anthropic
# トークン節約 > (3) ローカル LLM 仕事量。重労働は Ollama に押し付け、
# Claude には圧縮された精選情報のみを渡す。recall は広く取り、curate を
# heavy LLM (gemma3:12b) に任せ、最終 context は token-efficient に保つ。
TOP_K = int(os.environ.get("PERSONA_RECALL_TOP_K", "10"))
DISTANCE_MAX = float(os.environ.get("PERSONA_RECALL_DISTANCE_MAX", "1.0"))
DECAY_LAMBDA = float(os.environ.get("PERSONA_RECALL_DECAY_LAMBDA", "0.08"))
EMBED_TIMEOUT = float(os.environ.get("PERSONA_RECALL_TIMEOUT", "8.0"))
EPISODE_TOP_K = int(os.environ.get("PERSONA_RECALL_EPISODE_TOP_K", "10"))
EPISODE_DISTANCE_MAX = float(os.environ.get("PERSONA_RECALL_EPISODE_DISTANCE_MAX", "1.0"))
# 0 にすると非圧縮表示時の per-episode 切り捨てを完全にオフ (生発話を全部出す)。
EPISODE_SUMMARY_CAP = int(os.environ.get("PERSONA_RECALL_EPISODE_SUMMARY_CAP", "0"))
# 同 category で席が埋まり重要 fact が押し出されるのを防ぐ。0 なら制約なし。
PER_CATEGORY_CAP = int(os.environ.get("PERSONA_RECALL_PER_CATEGORY_CAP", "3"))

# LLM compression at *read time*. Per rule/memory_save_policy:
# write side stores everything raw; read side compresses on demand.
# Per rule/model_weight_policy: read-side compression uses the **heavy**
# model — its output goes directly into the main agent's context, so
# quality matters more than latency (fires only on large recall sets).
COMPRESS_MODEL = os.environ.get(
    "PERSONA_RECALL_COMPRESS_MODEL",
    os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b"),
)
# 圧縮タイムアウトは長めに。重量 LLM (gemma3:12b) の生成時間を許容。
COMPRESS_TIMEOUT = float(os.environ.get("PERSONA_RECALL_COMPRESS_TIMEOUT", "30.0"))
# 圧縮発火を早める (= ほぼ常に Ollama に curate させて Claude トークンを節約)。
# 1500 字超えたら圧縮、それ以下は生のままでも軽いので素通し。
COMPRESS_TRIGGER_CHARS = int(os.environ.get("PERSONA_RECALL_COMPRESS_TRIGGER", "1500"))
# 圧縮先は短め (Claude へ渡すトークン量を抑制)。ただし qualifier / 修飾語の
# 取りこぼしは禁止 (compress prompt 側で明示)。
COMPRESS_TARGET_CHARS = int(os.environ.get("PERSONA_RECALL_COMPRESS_TARGET", "1500"))
# Ollama 入力側は気前よく取る (作業は Ollama に押し付ける方針)。
COMPRESS_PER_EP_CAP = int(os.environ.get("PERSONA_RECALL_COMPRESS_PER_EP_CAP", "8000"))

# windows: ステージごとの age 上限 (日)。None は「無期限」、空リストは
# 「常に除外」(persona は SessionStart で全件注入済みなのでここでは出さない)。
# decay: importance-aware recency decay を適用するか。
CATEGORY_POLICY: dict[str, dict] = {
    "persona":    {"windows": [],              "decay": False},
    "profile":    {"windows": [None],          "decay": False},
    "rule":       {"windows": [None],          "decay": False},
    "skill":      {"windows": [365, None],     "decay": True},
    "preference": {"windows": [90, 365, None], "decay": True},
    "aversion":   {"windows": [90, 365, None], "decay": True},
    "context":    {"windows": [30, 90, 365],   "decay": True},
}
DEFAULT_POLICY = {"windows": [180, None], "decay": True}


def embed(text: str) -> bytes | None:
    try:
        r = httpx.post(
            f"{OLLAMA_HOST}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=EMBED_TIMEOUT,
        )
        r.raise_for_status()
        vec = r.json().get("embedding")
        if not isinstance(vec, list) or not vec:
            return None
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return struct.pack(f"{len(vec)}f", *vec)
    except Exception:
        return None


def _age_days(updated_at: str, now: datetime) -> float:
    """Compute age in days. DB stores timestamps in JST (datetime('now', '+9 hours')).

    Both `updated_at` and `now` are interpreted as JST so the subtraction
    yields a correct duration regardless of the host's local TZ.
    """
    try:
        dt = datetime.fromisoformat(updated_at).replace(tzinfo=JST)
    except Exception:
        return 0.0
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _effective_distance(raw: float, age_d: float, importance: int, decay: bool) -> float:
    if not decay:
        return raw
    weight = max(0.0, 1.0 - importance / 10.0)
    return raw + DECAY_LAMBDA * math.log1p(age_d / 365.0) * weight


def _open_db() -> sqlite3.Connection | None:
    if not DB_PATH or not Path(DB_PATH).exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def fetch_candidates(blob: bytes, fetch_k: int) -> list[dict]:
    conn = _open_db()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT f.category, f.key, f.value, f.importance, f.updated_at, v.distance
            FROM facts_vec v
            JOIN facts f ON f.id = v.fact_id
            WHERE v.embedding MATCH ?
              AND k = ?
              AND f.status = 'active'
              AND f.category != 'persona'
            ORDER BY v.distance
            """,
            (blob, fetch_k),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_episodes(blob: bytes, fetch_k: int) -> list[dict]:
    """Pull nearest past episodes (PreCompact / SessionEnd snapshots etc.).

    Episodes capture free-form discussion summaries that facts can't hold —
    decisions made, options weighed, current state of an in-progress task.
    They're recalled separately so a few of them can ride alongside facts
    without crowding them out.
    """
    conn = _open_db()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT e.id, e.role, e.summary, e.content, e.created_at, v.distance
            FROM episodes_vec v
            JOIN episodes e ON e.id = v.episode_id
            WHERE v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
            """,
            (blob, fetch_k),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def select_facts(candidates: list[dict], now: datetime, top_k: int = TOP_K) -> list[dict]:
    """category 別 window のカスケード + decay で候補を選別。

    各ステージで policy.windows[stage] を age 上限として適用し、
    threshold = DISTANCE_MAX + 0.1 * stage で effective_distance を絞る。
    top_k に届くまでステージを進める (届いた時点で打ち切り)。
    """
    for c in candidates:
        policy = CATEGORY_POLICY.get(c["category"], DEFAULT_POLICY)
        c["_age_d"] = _age_days(c["updated_at"], now)
        c["_policy"] = policy
        c["_eff"] = _effective_distance(
            c["distance"], c["_age_d"], c["importance"], policy["decay"]
        )

    max_stages = max(
        max((len(p["windows"]) for p in CATEGORY_POLICY.values()), default=1),
        len(DEFAULT_POLICY["windows"]),
        1,
    )

    selected: list[dict] = []
    seen: set[tuple[str, str]] = set()
    cat_count: dict[str, int] = {}
    for stage in range(max_stages):
        threshold = DISTANCE_MAX + 0.1 * stage
        pool: list[dict] = []
        for c in candidates:
            ck = (c["category"], c["key"])
            if ck in seen:
                continue
            windows = c["_policy"]["windows"]
            if not windows:
                continue
            cutoff = windows[stage] if stage < len(windows) else windows[-1]
            if cutoff is not None and c["_age_d"] > cutoff:
                continue
            if c["_eff"] > threshold:
                continue
            pool.append(c)
        pool.sort(key=lambda x: x["_eff"])
        # diversity 制約: 同 category 内で PER_CATEGORY_CAP まで先に取り、
        # 余った席があれば 2 周目で cap を緩めて埋める。これで profile/basic
        # のような単独で重要な fact が skill 系に席を奪われない。
        for c in pool:
            if len(selected) >= top_k:
                break
            cat = c["category"]
            if PER_CATEGORY_CAP and cat_count.get(cat, 0) >= PER_CATEGORY_CAP:
                continue
            selected.append(c)
            seen.add((c["category"], c["key"]))
            cat_count[cat] = cat_count.get(cat, 0) + 1
        if len(selected) >= top_k:
            break

    # 残席があれば cap を無視してでも埋める (取りこぼし > 偏り問題)
    if len(selected) < top_k:
        for c in candidates:
            if len(selected) >= top_k:
                break
            ck = (c["category"], c["key"])
            if ck in seen:
                continue
            if c["_eff"] > DISTANCE_MAX + 0.1 * (max_stages - 1):
                continue
            selected.append(c)
            seen.add(ck)

    return selected


def compress_episodes(user_prompt: str, episodes: list[dict]) -> str | None:
    """Filter+compress voluminous episode recall via local LLM.

    Returns None on any failure (caller falls back to per-line truncation).
    Used only when raw episode total content exceeds COMPRESS_TRIGGER_CHARS —
    truncating each raw_user / raw_assistant / raw_dump episode at 240 chars
    would otherwise destroy their meaning, so we ask the local model to
    distill the parts directly relevant to the user's current prompt.
    """
    if not episodes:
        return None
    parts: list[str] = []
    for e in episodes:
        body = (e.get("content") or e.get("summary") or "").strip()
        if not body:
            continue
        if len(body) > COMPRESS_PER_EP_CAP:
            body = body[:COMPRESS_PER_EP_CAP] + "…[truncated]"
        role = e.get("role") or "?"
        parts.append(f"[{e['created_at']} role={role}]\n{body}")
    if not parts:
        return None
    full = "\n\n---\n\n".join(parts)

    user_excerpt = user_prompt.strip()
    if len(user_excerpt) > 600:
        user_excerpt = user_excerpt[:600] + "…"

    prompt = (
        f"ユーザーの今の発話:\n{user_excerpt}\n\n"
        "以下は記憶DBから引いた過去の会話記録 (生発話・要約・スナップショット混在) です。"
        f"ユーザーの今の発話に直接関連する核心情報のみを {COMPRESS_TARGET_CHARS} 文字以内の"
        "日本語で要約してください。\n\n"
        "**必ず拾うべき情報**:\n"
        "- アシスタントが過去に行った具体的作業 (実装内容・編集ファイル・実行コマンド・修正点)\n"
        "- ユーザーから出た指示・決定・好み・属性\n"
        "- 進行中タスクの現状と次に予定している作業\n"
        "- 未解決の問題・エラー\n\n"
        "**正確性ルール (絶対遵守)**:\n"
        "- **修飾語・限定句・但し書きを絶対に省略しない**。\n"
        "  例: 「PM 含めて 8 人」の『PM 含めて』、「Java は 5 年だが避けたい派」の『が避けたい派』、\n"
        "  「2026-05-02 時点では」の日付限定、「前職で」「現職で」の所属限定 を必ず保持。\n"
        "- **数値はそのまま保持** (元の数字を変えない、丸めない、概算化しない)。\n"
        "- **否定・忌避の表現を肯定形に書き換えない**。\n"
        "  「Java は書きたくない」を「Java も書ける」と書いたら NG。元の極性を保つ。\n"
        "- **訂正された古い情報を残さない**。「みく → さくらに訂正済み」のような場合、\n"
        "  最新の正解 (さくら) のみ書き、古い誤情報 (みく) は混ぜない。\n\n"
        "**省くもの**: 雑談・繰り返し・本題と無関係な部分。\n"
        "**禁止**: 会話に書かれていない情報を補完・推測しない。本文のみ出力 (前置き・後書き不要)。\n\n"
        f"記録:\n{full}\n\n要約:"
    )
    try:
        r = httpx.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": COMPRESS_MODEL, "prompt": prompt, "stream": False},
            timeout=COMPRESS_TIMEOUT,
        )
        r.raise_for_status()
        text = (r.json().get("response") or "").strip()
        return text if text else None
    except Exception:
        return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return

    # ── Secret guard: block before the prompt reaches Anthropic's servers ──
    secrets = detect_secrets(prompt)
    if secrets:
        sys.stderr.write(
            "⚠️  シークレット（APIキー / 高エントロピートークン）を検出しました。\n"
            f"  検出箇所: {', '.join(secrets)}\n\n"
            "このプロンプトは Anthropic のサーバーには送信されませんでした。\n\n"
            "安全な手順:\n"
            "  1. .env に保存する  →  MY_KEY=<値>\n"
            "  2. 会話では「.env の MY_KEY に入れた」とキー名だけ伝える\n"
        )
        sys.exit(2)
    # ───────────────────────────────────────────────────────────────────────

    blob = embed(prompt)
    if not blob:
        # fail-loud: Ollama 落ち / embedding 失敗時はメインエージェントが
        # 「記憶 recall がオフライン」と認識できるように additionalContext で
        # 通知する (silent 死を防ぐ)。stderr にも一行残す。
        sys.stderr.write(
            "[persona-memory] Ollama embedding failed; recall offline this turn\n"
        )
        warn = (
            "## persona-memory 警告\n"
            "Ollama 接続失敗または embedding エラーのため、今ターンは記憶 recall が "
            "無効化されています。`ollama serve` の起動状態を確認してください。"
        )
        out = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": warn,
            }
        }
        print(json.dumps(out, ensure_ascii=False))
        return
    try:
        candidates = fetch_candidates(blob, max(TOP_K * 4, 20))
    except Exception:
        candidates = []
    try:
        episodes = fetch_episodes(blob, max(EPISODE_TOP_K * 3, 6))
    except Exception:
        episodes = []

    now = datetime.now(JST)
    facts = (
        [c for c in select_facts(candidates, now) if c["distance"] <= DISTANCE_MAX + 0.5]
        if candidates
        else []
    )
    eps = [e for e in episodes if e["distance"] <= EPISODE_DISTANCE_MAX][:EPISODE_TOP_K]

    if not facts and not eps:
        return

    sections: list[str] = []
    if facts:
        lines = [f"## 関連する記憶 (この発話に紐づく fact: {len(facts)} 件)"]
        for f in facts:
            eff_note = (
                f", eff={f['_eff']:.3f}"
                if abs(f["_eff"] - f["distance"]) > 0.001
                else ""
            )
            lines.append(
                f"- [{f['category']}/{f['key']}] "
                f"(importance={f['importance']}, distance={f['distance']:.3f}{eff_note})\n"
                f"  {f['value']}"
            )
        sections.append("\n".join(lines))

    if eps:
        # Compute total content size to decide whether to invoke LLM compression.
        # raw_user / raw_assistant / raw_dump episodes can be much larger than
        # the legacy summary episodes; per-line truncation would shred them.
        total_chars = sum(
            len(e.get("content") or e.get("summary") or "") for e in eps
        )
        compressed: str | None = None
        if total_chars >= COMPRESS_TRIGGER_CHARS:
            compressed = compress_episodes(prompt, eps)

        if compressed:
            header = (
                f"## 関連する過去の議論 (圧縮要約: episode {len(eps)} 件 / "
                f"{total_chars} 字 → LLM 距離フィルタ)"
            )
            sections.append(f"{header}\n{compressed}")
        else:
            lines = [f"## 関連する過去の議論 (episode: {len(eps)} 件)"]
            for e in eps:
                # rule/recall_priority: content (生発話の全文) を優先。
                # summary は judge が削った要約版なので情報量が少ない。
                text = (e.get("content") or e.get("summary") or "").strip()
                if EPISODE_SUMMARY_CAP and len(text) > EPISODE_SUMMARY_CAP:
                    text = text[:EPISODE_SUMMARY_CAP] + "…"
                # 改行は保持 (raw 発話の構造を残す)。先頭にインデントを付与。
                indented = "\n  ".join(text.splitlines())
                lines.append(
                    f"- [{e['created_at']}] (distance={e['distance']:.3f})\n  {indented}"
                )
            sections.append("\n".join(lines))

    context = "\n\n".join(sections)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
