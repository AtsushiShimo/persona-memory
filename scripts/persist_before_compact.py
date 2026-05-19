#!/usr/bin/env python3
"""PreCompact hook: snapshot the current conversation as an episode before
Claude Code compacts (or the user issues /compact). This persists the
durable parts of what was just discussed into the persona-memory DB so they
survive after the live context shrinks.

Reads the hook payload from stdin (Claude Code provides session_id,
transcript_path, trigger), summarizes recent turns via Ollama, and writes
an episode through the same DB module the MCP server uses.

Fail-open: any error -> exit 0 silently so compaction proceeds normally.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import httpx  # noqa: F401  (used inside summarize())
except Exception:
    sys.exit(0)

from server import db, embedding  # noqa: E402

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# Per rule/model_weight_policy: PreCompact / SessionEnd write a summary that
# will be re-compressed at recall time anyway, so we use the **light** model
# here. The raw_dump episode (saved alongside) preserves the full turns for
# read-time heavy compression to work from.
JUDGE_MODEL = os.environ.get(
    "PERSONA_JUDGE_MODEL",
    os.environ.get("PERSONA_LIGHT_MODEL", "gemma3:4b"),
)
RECENT_TURNS = int(os.environ.get("PERSONA_COMPACT_RECENT_TURNS", "40"))
PER_MESSAGE_CAP = int(os.environ.get("PERSONA_COMPACT_PER_MSG_CAP", "2000"))
TOTAL_CAP = int(os.environ.get("PERSONA_COMPACT_TOTAL_CAP", "32000"))
SUMMARY_TIMEOUT = float(os.environ.get("PERSONA_COMPACT_SUMMARY_TIMEOUT", "60.0"))
# 生 dump 時の 1 メッセージ上限 (文字数)。研究系 tool_result を切らないため
# auto_persist.py と同じ 128KB を既定とする。低スペック機向けに env で下げ可能。
RAW_PER_MESSAGE_CAP = int(os.environ.get("PERSONA_COMPACT_RAW_CAP", "131072"))

# Resolve the persona's display name from DB (no hard-coded names in source).
ASSISTANT_NAME = db.get_persona_name(default="アシスタント")


def _flatten_tool_result(tc) -> str:
    """tool_result content can be a string, a list of typed blocks, or
    richer (image / json). Flatten to a single string for storage."""
    if isinstance(tc, str):
        return tc
    if isinstance(tc, list):
        parts: list[str] = []
        for item in tc:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", "") or "")
                elif item.get("type") == "image":
                    parts.append("[image omitted]")
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(tc)


def read_transcript(path: str) -> list[dict]:
    """Parse a Claude Code JSONL transcript.

    Captures *all* user/assistant content blocks (text + tool_use + tool_result)
    so research-style work (WebFetch, WebSearch, Read, Bash) is preserved with
    its source data intact, not just the assistant's summary text.
    """
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") not in ("user", "assistant"):
                    continue
                m = msg.get("message") or {}
                role = m.get("role", "")
                content = m.get("content", "")
                if isinstance(content, list):
                    parts: list[str] = []
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        ctype = c.get("type")
                        if ctype == "text":
                            t = c.get("text", "")
                            if t:
                                parts.append(t)
                        elif ctype == "tool_use":
                            tname = c.get("name", "?")
                            tinput = c.get("input", {})
                            try:
                                tinput_s = json.dumps(tinput, ensure_ascii=False)
                            except Exception:
                                tinput_s = str(tinput)
                            parts.append(f"[tool_use:{tname}] {tinput_s}")
                        elif ctype == "tool_result":
                            tid = c.get("tool_use_id", "?")
                            body = _flatten_tool_result(c.get("content", ""))
                            if body.strip():
                                is_err = c.get("is_error")
                                tag = "tool_error" if is_err else "tool_result"
                                parts.append(f"[{tag}:{tid}]\n{body}")
                    content = "\n\n".join(parts)
                if role and isinstance(content, str) and content.strip():
                    out.append({"role": role, "content": content})
    except Exception:
        return []
    return out


async def summarize(messages: list[dict]) -> str:
    """Ask the local judge model to summarize recent turns in Japanese."""
    if not messages:
        return ""
    import httpx as _httpx

    lines = [f"[{m['role']}] {m['content'][:PER_MESSAGE_CAP]}" for m in messages]
    convo = "\n".join(lines)
    if len(convo) > TOTAL_CAP:
        convo = convo[-TOTAL_CAP:]

    prompt = (
        f"以下は人間 (User) と AI アシスタント (Assistant, ペルソナ名: {ASSISTANT_NAME}) の会話抜粋です。"
        "コンテキストがコンパクトされる前に、後で「最後に何をしていたか」を"
        "確実に思い出せるよう、500 字程度の日本語で箇条書き風に要約してください。\n\n"
        "**必ず含めること**:\n"
        "- 決定事項・指示・好み・進行中のタスク\n"
        "- **アシスタント自身が実際に行った作業 (編集したファイル、実行したコマンド、追加/修正した実装内容)** — "
        "これは出力ログではなく「やったこと」であり、再開時の核心情報。\n"
        "- 未解決の問題・次にやる予定の作業\n\n"
        "雑談的な部分・単独の挨拶のみ省いて構いません。\n\n"
        f"{convo}\n\n要約:"
    )
    try:
        async with _httpx.AsyncClient(timeout=SUMMARY_TIMEOUT) as client:
            r = await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={
                    "model": JUDGE_MODEL,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            r.raise_for_status()
    except Exception:
        return ""
    return (r.json().get("response") or "").strip()


async def persist_raw_dump(
    session_id: str, kind: str, recent: list[dict], turns_total: int
) -> None:
    """直近ターンの生発話を 1 つの episode に丸ごと保存する。

    記憶ポリシー (rule/memory_save_policy): 書き込み時は要約や切り捨てをせず
    全部保存。要約は別 episode として併設する (要約だけで生発話が消えないよう)。
    LLM 介在なし — 失敗してもここで原文が DB に残る。
    """
    if not recent:
        return
    parts: list[str] = []
    for m in recent:
        role = (m.get("role") or "").strip()
        content = (m.get("content") or "").strip()
        if not role or not content:
            continue
        if len(content) > RAW_PER_MESSAGE_CAP:
            content = content[:RAW_PER_MESSAGE_CAP] + "…[truncated]"
        parts.append(f"[{role}]\n{content}")
    if not parts:
        return
    body = "\n\n---\n\n".join(parts)
    header = (
        f"{kind} raw_dump (turns_total={turns_total}, turns_dumped={len(parts)})"
    )
    full = f"{header}\n\n{body}"
    try:
        ep_id = db.append_episode(
            session_id=session_id,
            role="raw_dump",
            content=full,
            summary=header,
        )
    except Exception:
        return
    # 埋め込みは末尾 2KB ぶんで取る (検索ヒット用。content 全文を埋め込むのは過剰)。
    try:
        sample = body[-2000:] if len(body) > 2000 else body
        vec = await embedding.embed_text(sample)
        db.write_episode_embedding(ep_id, embedding.l2_normalize(vec))
    except Exception:
        pass


async def main_async() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    session_id = (payload.get("session_id") or "")[:64]
    transcript_path = payload.get("transcript_path") or ""
    trigger = payload.get("trigger") or payload.get("reason") or "auto"
    # Snapshot kind comes from the wrapper script via env so the same Python
    # entry can serve both PreCompact and SessionEnd.
    kind = os.environ.get("PERSONA_SNAPSHOT_KIND", "PreCompact")

    if not transcript_path or not Path(transcript_path).exists():
        return

    messages = read_transcript(transcript_path)
    if not messages:
        return
    recent = messages[-RECENT_TURNS:]

    # 生 dump のみ保存 (rule/memory_save_policy: 書き込みは生のまま、
    # 要約は読み出し時 = compress_episodes に任せる)。
    # 書き込み時要約はトークン節約と将来の詳細復元の両面で逆効果なので廃止。
    await persist_raw_dump(session_id, kind, recent, len(messages))

    # SessionEnd でだけ episodes TTL GC を走らせる。PreCompact 中は context が
    # 詰まりがちで余計な仕事を増やしたくない。raw 系は 30 日で消し、summary は
    # 永続 (None) — 数年単位プロジェクトで「3 年前の決定」を引けるようにする。
    # summary は短いのでストレージ的に永続でも実害なし。
    if kind == "SessionEnd":
        try:
            r = db.gc_episodes(raw_ttl_days=30, summary_ttl_days=None)
            if r["raw_deleted"] or r["summary_deleted"]:
                print(
                    f"[gc] deleted {r['raw_deleted']} raw, "
                    f"{r['summary_deleted']} summary",
                    file=sys.stderr,
                )
        except Exception:
            pass


def main() -> None:
    try:
        asyncio.run(main_async())
    except Exception:
        pass


if __name__ == "__main__":
    main()
