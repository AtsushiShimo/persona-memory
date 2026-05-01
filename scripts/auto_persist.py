#!/usr/bin/env python3
"""Stop hook: adaptively persist conversation turns into the persona DB.

Fires after each assistant turn but **only acts** once enough new turns
have accumulated since the last snapshot for this session. When it does
act, it asks a local Ollama model to extract:

  1. New `facts` (category/key/value/importance) worth durable storage.
  2. An optional `episode` summary capturing decisions, proposals, or
     rejected options from the recent slice.

Both are written through the same `server.db` module the MCP server uses,
so they participate in vector recall on subsequent prompts.

Fail-open: any error -> exit 0 silently so Stop processing proceeds.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import httpx  # noqa: F401
except Exception:
    sys.exit(0)

from server import db, embedding  # noqa: E402

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# Per rule/model_weight_policy: Stop hook fires every assistant turn, so the
# judge defaults to the **light** model. Quality is acceptable because raw
# turns are saved unconditionally — the judge is best-effort fact extraction.
# PERSONA_JUDGE_MODEL takes precedence for back-compat; PERSONA_LIGHT_MODEL
# is the new canonical setting.
JUDGE_MODEL = os.environ.get(
    "PERSONA_JUDGE_MODEL",
    os.environ.get("PERSONA_LIGHT_MODEL", "gemma3:4b"),
)
TURN_THRESHOLD = int(os.environ.get("PERSONA_AUTO_PERSIST_THRESHOLD", "1"))
PER_MESSAGE_CAP = int(os.environ.get("PERSONA_AUTO_PERSIST_PER_MSG_CAP", "2000"))
TOTAL_CAP = int(os.environ.get("PERSONA_AUTO_PERSIST_TOTAL_CAP", "24000"))
JUDGE_TIMEOUT = float(os.environ.get("PERSONA_AUTO_PERSIST_TIMEOUT", "60.0"))
# 生ターン保存時の 1 メッセージあたり上限 (バイト数ではなく文字数)。
# 「全部保存」を謳う以上、研究系の tool_result (WebFetch 50-100KB / Read 大規模
# ファイル / Bash の長文 stdout) を切り捨てないだけのサイズが必要。
# sqlite は GB スケールまで余裕で扱えるのでストレージは制約にならない。
# embedding は別途先頭 2KB のみ取るので embedding コストもサイズ非依存。
# 環境変数で下げたい場合 (低スペック機等) は PERSONA_AUTO_PERSIST_RAW_CAP で上書き。
RAW_PER_MESSAGE_CAP = int(os.environ.get("PERSONA_AUTO_PERSIST_RAW_CAP", "131072"))

# Resolve the persona's display name from DB at module load. Distributable
# scripts must not hard-code a specific persona name in source — each install
# has its own (or none yet, in which case we use a generic label).
ASSISTANT_NAME = db.get_persona_name(default="アシスタント")
# auto_persist 由来の fact は誤検知率が手動 write_fact より高いので importance を
# 抑える。ユーザーが明示的に強調した場合は手動の write_fact で 7+ を上書き可能。
IMPORTANCE_CAP = int(os.environ.get("PERSONA_AUTO_PERSIST_IMPORTANCE_CAP", "6"))

VALID_CATEGORIES = {
    "persona",
    "preference",
    "rule",
    "profile",
    "skill",
    "context",
}

MIN_VALUE_LEN = int(os.environ.get("PERSONA_AUTO_PERSIST_MIN_VALUE_LEN", "15"))

# システム自体に言及する語。これらが混じる fact は要約モデルが
# 「システムの説明」を fact 化してしまった疑いが強い (rule/system_memory_restriction)。
SYSTEM_TERMS = (
    "search_memory",
    "lint_memory",
    "write_fact",
    "append_episode",
    "auto_persist",
    "proxy_recall",
    "persist_before_compact",
    "on-session-start",
    "on-session-end",
    "on-pre-compact",
    "on-user-prompt",
    "settings.json",
    ".mcp.json",
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreCompact",
    "Stop フック",
    "judge",
    "embedding",
    "gemma3",
    "nomic-embed",
    "sqlite-vec",
    "init.sh",
    "setup.sh",
    "new-persona.sh",
    "seed_persona",
    "init-memory",
    "ollama",
    "Ollama",
)

# 文字化け・壊れ value の兆候。逆スラ + 句読点、制御文字、連続バッククオート、など。
BROKEN_VALUE_RE = re.compile(r"[\\][,。、・\s]|[\x00-\x08\x0b-\x1f]|`{2,}")

# アシスタント自身に関する自己言及や行動ログ。fact ではなく episode に行くべき。
# ペルソナ名は配布物にハードコードしたくないので動的に組み立てる。
_self_ref_parts = [r"^(私|わたし)(は|が)", r"を提案している", r"を検討している", r"することを提案"]
if ASSISTANT_NAME and ASSISTANT_NAME != "アシスタント":
    _self_ref_parts.insert(0, re.escape(ASSISTANT_NAME))
SELF_REF_RE = re.compile("|".join(_self_ref_parts))

# TODO / 願望 / 行動予定形 — fact ではなくエピソードに属する。
TODO_RE = re.compile(
    r"必要がある|予定である|確認する必要|調査する必要|を試す|を検討|することを促"
)

# 他の fact を category/key 形式で参照する meta-fact (例: "preference/lint_usage は…")。
# 自己参照ループの典型パターン。
META_REF_RE = re.compile(
    r"(persona|preference|rule|profile|skill|context)/[a-z][a-z0-9_]*"
)

# 進捗状況・一時的ステータス (DL 進行中、%、サイズ単位、ETA など)。
EPHEMERAL_RE = re.compile(r"進行中|残り\s*\d|\d+\s*%|\d+\s*[KMG]?B\b|ETA")

# persona / profile は短い原子的属性 (例: "敬語 (丁寧)"、"探究心旺盛"、"性別: 女性") が
# 正当に存在するので最小長チェックから除外する。
SHORT_OK_CATEGORIES = ("persona", "profile")


def is_noise_value(category: str, value: str) -> tuple[bool, str]:
    """Heuristic post-validation for auto_persist-extracted fact values.

    Returns (is_noise, reason). Reason is for debugging only.
    """
    v = value.strip()
    if len(v) < MIN_VALUE_LEN and category not in SHORT_OK_CATEGORIES:
        return True, f"too_short<{MIN_VALUE_LEN}"
    if BROKEN_VALUE_RE.search(v):
        return True, "broken_chars"
    if SELF_REF_RE.search(v):
        return True, "self_reference"
    if TODO_RE.search(v):
        return True, "todo_or_intention"
    if META_REF_RE.search(v):
        return True, "meta_fact_reference"
    # rule / preference / persona は system 用語を含めば即弾く (一般知識化されたシステム説明)。
    # context は長文なら project 状態として許容するが、複数の system 用語が混じる場合は
    # 仕組み解説 / リリースノート的になっていて記憶価値が薄い (≥2 hits)。
    # 短文 (< 60 字) で system 用語が混じる場合も同様に弾く。
    system_hits = sum(1 for term in SYSTEM_TERMS if term in v)
    if system_hits:
        if category in ("rule", "preference", "persona"):
            return True, "system_term_in_non_context"
        if category == "context":
            if len(v) < 60:
                return True, "short_system_explanation"
            # 複数 system 用語の同時出現は仕組み解説の典型 (リリースノート的)。
            # ただし長文の project 決定記録 (importance キャップ変更等) も
            # 2 hit してしまうので、3+ ヒットに引き上げて安全側に倒す。
            if system_hits >= 3:
                return True, "multiple_system_terms"
    # 進捗状況・一時的ステータス (例: "pull は進行中 9% / 8.1GB / 約 10 分") は
    # 数日で陳腐化する ephemeral 情報。fact ではなく episode に行くべき。
    if EPHEMERAL_RE.search(v):
        return True, "ephemeral_status"
    return False, ""


def _flatten_tool_result(tc) -> str:
    """tool_result content can be a string, a list of {type:text, text:...}
    blocks, or richer (image, json). Flatten to a string for storage."""
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

    Captures *all* user/assistant content blocks, not just text:
      - text         → as-is
      - tool_use     → "[tool_use:<name>] <input>"  (what Claude asked the tool to do)
      - tool_result  → "[tool_result:<tool_use_id>] <body>"  (what the tool returned)

    This matters because research-style work (WebFetch / WebSearch / Read /
    Bash) keeps its actual data in tool_result blocks. Dropping them would
    leave only the assistant's summary in memory — the original source is
    gone, so future sessions can't verify or follow up on the details.
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


def get_meta(key: str) -> str | None:
    try:
        with db.connect() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None
    except Exception:
        return None


def set_meta(key: str, value: str) -> None:
    try:
        with db.connect() as c:
            c.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    except Exception:
        pass


def slice_for_judge(messages: list[dict]) -> str:
    lines = [f"[{m['role']}] {m['content'][:PER_MESSAGE_CAP]}" for m in messages]
    convo = "\n".join(lines)
    if len(convo) > TOTAL_CAP:
        convo = convo[-TOTAL_CAP:]
    return convo


JUDGE_PROMPT_TEMPLATE = """以下は人間 (User) と AI アシスタント (Assistant, ペルソナ名: {assistant_name}) の会話抜粋です。
継続的な会話の一部であり、後で別セッションでも思い出すべき情報を抽出します。
判断に迷ったら記憶する側に倒すが、**捏造は厳禁**。

**厳密な JSON のみ** で返してください (前置きや説明禁止):

{{
  "facts": [
    {{
      "category": "preference|rule|profile|skill|context|persona のいずれか",
      "key": "snake_case の安定識別子 (例: editor, main_languages, ci_cache_strategy)",
      "value": "簡潔な事実 (1-2 文)。会話に直接書かれていることだけ",
      "importance": 1-6 の整数,
      "reason": "なぜ記憶すべきか短く"
    }}
  ],
  "episode": {{
    "worth_saving": true または false,
    "summary": "重要な議論の要約 (200-300 字)。決定事項・提案・却下案とその理由を含む"
  }}
}}

【絶対ルール (違反したら全フィールド空で返す)】
1. **会話に明示されていない理由・動機・選択理由を捏造しない**。
   例: ユーザーが「Sonnet にして」と言っただけなら、選択理由 (コスト等) は不明。書かない。
2. **会話外のドメイン知識・形容詞を付け足さない**。
   「最も高性能な」「最新の」「業界標準の」など、会話に書かれていない属性は禁止。
   固有名詞には会話に書かれた説明だけを添える (会話で「Sonnet 4.6」とだけ言及されたら、それ以上の説明をするな)。
3. **システム自体に関する説明は記憶しない**。
   recall の見方・hook の仕組み・slash コマンドの操作手順・モデル切替の手順 → 全部 NG (一般知識)。
4. **一時的な選択を「ユーザーの嗜好」として記録しない**。
   "今このモデルにして" ≠ "永続的にこのモデルを好む"。preference は **本人が一般化した嗜好** だけ。
5. **system-reminder や recall の中身そのものを fact 化しない** (再帰的ノイズになる)。

【category の使い分け (厳密に)】
- `rule` = **ハード制約** のみ (例: "main に force push 禁止"、"PII を log に出さない")。手順・操作方法は rule **ではない**
- `preference` = ユーザーの**安定した嗜好** (例: "neovim 派"、"英語コミット")
- `profile` = ユーザー属性 (役職・住居・所有物・関係)
- `skill` = ユーザーのスキル・経験
- `context` = 現在進行中の固有名詞付きタスクや状況 (例: "proxy_recall.py 改修中")。一般論や警告は context **ではない**
- `persona` = アシスタント自身への振る舞い指示 (ユーザーがアシスタントに対して指示したルール)

【記憶不要 (記録するな)】
- 雑談・挨拶・「了解」のみの単独確認
- 重複・既知 (同じ情報が直近で記録されているなら再記録しない)

【記憶すべき (拾え) — 迷ったら拾う側に倒す】
- ユーザーが新たに表明した嗜好・属性・スキル・進行中タスク
- ユーザーが私に下した指示 (振る舞いルール → persona)
- 設計判断・パラメータ確定・却下案とその理由 → episode
- 私が出した推奨案で**ユーザーが採択を明言したもの** → episode
- **アシスタント自身が実際に行った作業 (ファイル編集・コマンド実行・実装内容・修正点) → episode**
  これらは出力ログではなく「やったこと」であり、後で「最後に何をしていたか」を
  思い出すための核心情報。要約に含めること。

会話抜粋:
---
{convo}
---

JSON:
"""


def parse_json_loose(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


async def judge(messages: list[dict]) -> dict | None:
    if not messages:
        return None
    import httpx as _httpx

    convo = slice_for_judge(messages)
    prompt = JUDGE_PROMPT_TEMPLATE.format(convo=convo, assistant_name=ASSISTANT_NAME)
    try:
        async with _httpx.AsyncClient(timeout=JUDGE_TIMEOUT) as client:
            r = await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={
                    "model": JUDGE_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
            )
            r.raise_for_status()
    except Exception:
        return None
    raw = (r.json().get("response") or "").strip()
    return parse_json_loose(raw)


async def persist_facts(facts: list[dict]) -> int:
    saved = 0
    for f in facts:
        try:
            category = (f.get("category") or "").strip().lower()
            key = (f.get("key") or "").strip()
            value = (f.get("value") or "").strip()
            importance = int(f.get("importance") or 5)
            if category not in VALID_CATEGORIES or not key or not value:
                continue
            if not re.match(r"^[a-z][a-z0-9_]*$", key):
                continue
            noise, _ = is_noise_value(category, value)
            if noise:
                continue
            # auto_persist 由来は cap で抑える。手動 write_fact なら上書き可能。
            importance = max(1, min(IMPORTANCE_CAP, importance))
            fact_id = db.upsert_fact(
                category=category,
                key=key,
                value=value,
                importance=importance,
                source="auto_persist",
            )
            try:
                vec = await embedding.embed_text(value)
                db.write_fact_embedding(fact_id, embedding.pack_embedding(vec))
            except Exception:
                pass
            saved += 1
        except Exception:
            continue
    return saved


async def persist_raw_turns(session_id: str, messages: list[dict]) -> int:
    """新規ターンを生のまま episode として無条件 append。

    記憶ポリシー (rule/memory_save_policy): 書き込み時は要約や切り捨てをせず
    全部保存する。要約・取捨選択は読み出し時の責務。LLM 判定は介在させない。
    """
    saved = 0
    for m in messages:
        role = (m.get("role") or "").strip()
        content = (m.get("content") or "").strip()
        if not role or not content:
            continue
        # 極端に長い場合のみ末尾を切る (16KB)。普通の発話はそのまま入る。
        if len(content) > RAW_PER_MESSAGE_CAP:
            content = content[:RAW_PER_MESSAGE_CAP] + "…[truncated]"
        try:
            ep_id = db.append_episode(
                session_id=session_id,
                role=f"raw_{role}",
                content=content,
                summary=content[:200],
            )
        except Exception:
            continue
        try:
            vec = await embedding.embed_text(content[:2000])
            db.write_episode_embedding(ep_id, embedding.pack_embedding(vec))
        except Exception:
            pass
        saved += 1
    return saved


async def persist_episode(session_id: str, summary: str) -> bool:
    summary = (summary or "").strip()
    if not summary:
        return False
    try:
        ep_id = db.append_episode(
            session_id=session_id,
            role="system",
            content=f"AutoPersist snapshot\n\n{summary}",
            summary=summary,
        )
    except Exception:
        return False
    try:
        vec = await embedding.embed_text(summary)
        db.write_episode_embedding(ep_id, embedding.pack_embedding(vec))
    except Exception:
        pass
    return True


async def main_async() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return

    session_id = (payload.get("session_id") or "")[:64] or "unknown"
    transcript_path = payload.get("transcript_path") or ""
    if not transcript_path or not Path(transcript_path).exists():
        return

    messages = read_transcript(transcript_path)
    if not messages:
        return

    meta_key = f"auto_persist_last_idx:{session_id}"
    last_raw = get_meta(meta_key)
    try:
        last_idx = int(last_raw) if last_raw is not None else 0
    except ValueError:
        last_idx = 0

    new_count = len(messages) - last_idx
    if new_count < TURN_THRESHOLD:
        return

    # 1) **生ターン保存 (最優先・無条件)**: 新規ターンを raw_user / raw_assistant
    #    として episode に書き込む。LLM 判定は通さない — 失敗してもここで全発話が
    #    DB に残る。look_back は不要 (重複保存になる)。
    new_msgs = messages[last_idx:]
    await persist_raw_turns(session_id, new_msgs)

    # 2) **judge による fact 抽出 + 補助要約 (best-effort)**: 失敗しても 1) で
    #    生発話は守られているので情報は欠落しない。look_back で参照解決を助ける。
    look_back = 2
    start = max(0, last_idx - look_back)
    slice_msgs = messages[start:]

    result = await judge(slice_msgs)
    if result and isinstance(result, dict):
        facts = result.get("facts") or []
        episode = result.get("episode") or {}
        if isinstance(facts, list):
            await persist_facts(facts)
        # worth_saving フィルタは廃止: 要約があれば常に保存。要約の取捨は読み出し側で。
        if isinstance(episode, dict):
            summary = (episode.get("summary") or "").strip()
            if summary:
                await persist_episode(session_id, summary)

    set_meta(meta_key, str(len(messages)))


def main() -> None:
    try:
        asyncio.run(main_async())
    except Exception:
        pass


if __name__ == "__main__":
    main()
