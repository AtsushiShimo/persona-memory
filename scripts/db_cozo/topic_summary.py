"""topic ごとの「生きてる話題箱」 summary を LLM で生成・更新する.

設計の核 (0.7.3):
- 新 topic を作るとき → 発話 1 件を起点に短い summary を生成
- 既存 topic に新発話を紐付けるとき → 旧 summary + 新発話を渡して更新版を生成
- summary は短く (= 並列の topic を埋めるとき embedding 比較しやすくする目的).
  100-200 字目安.

軽量モデル (gemma3:4b 等) を使い、 リアルタイム経路をブロックしすぎない.
"""
from __future__ import annotations

import os

from scripts.shared.ollama import LLMClient

SUMMARY_MODEL = os.environ.get(
    "PERSONA_TOPIC_SUMMARY_MODEL",
    os.environ.get("PERSONA_LIGHT_MODEL", "gemma3:4b"),
)
SUMMARY_NUM_CTX = int(os.environ.get("PERSONA_TOPIC_SUMMARY_NUM_CTX", "4096"))
SUMMARY_MAX_CHARS = int(os.environ.get("PERSONA_TOPIC_SUMMARY_MAX_CHARS", "200"))


_INIT_PROMPT_TEMPLATE = """\
以下は会話の冒頭発話です. この発話が **何の話題** を扱っているかを
**{max_chars} 字以内の日本語 1 文** で要約してください.

要件:
- 話題の主題語 (機能名・固有名詞・対象物) を必ず含める
- 「ユーザーは〜」「アシスタントが〜」 等の主語を避け、 話題そのものを書く
- 推測・前置き・「と思われる」 等は禁止
- 出力は要約本文のみ. 前置き・コードフェンス・JSON 等不要

発話 ({role}):
{content}
"""


_UPDATE_PROMPT_TEMPLATE = """\
以下は今扱っている話題の **現在の要約** と、 新たに加わった発話です.
新発話を反映して、 話題の要約を **{max_chars} 字以内の日本語 1 文** に
**更新** してください.

要件:
- 主題語 (機能名・固有名詞・対象物) を保持
- 新発話で話の流れが進んだ点があれば取り込む
- 旧要約を全否定しない (= 同じ話題の継続なので、 主題は維持)
- 推測・前置き・「と思われる」 等は禁止
- 出力は更新後の要約本文のみ. 前置き・コードフェンス・JSON 等不要

現在の要約:
{old_summary}

新発話 ({role}):
{content}
"""


def _clean(text: str, max_chars: int) -> str:
    """LLM 出力を要約本文として整形 (改行折りたたみ・冗長削除・字数制限)."""
    if not text:
        return ""
    s = text.strip()
    # コードフェンスや前置きを軽く剥がす
    if s.startswith("```"):
        s = s.strip("`")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # 複数行になっていれば最初の段落 (空行までの塊) を採用
    parts = [p.strip() for p in s.split("\n\n") if p.strip()]
    if parts:
        s = parts[0]
    s = " ".join(line.strip() for line in s.split("\n") if line.strip())
    if len(s) > max_chars:
        s = s[:max_chars].rstrip()
    return s


def generate_initial_summary(
    role: str,
    content: str,
    llm: LLMClient,
    model: str = SUMMARY_MODEL,
    max_chars: int = SUMMARY_MAX_CHARS,
) -> str:
    """新 topic 作成時の初期 summary 生成.

    LLM 失敗時は発話冒頭をそのまま縮めて fallback.
    """
    if not content.strip():
        return ""
    prompt = _INIT_PROMPT_TEMPLATE.format(
        role=role, content=content[:1000], max_chars=max_chars,
    )
    try:
        response = llm.generate(model, prompt, num_ctx=SUMMARY_NUM_CTX)
    except Exception:
        response = ""
    cleaned = _clean(response, max_chars)
    if cleaned:
        return cleaned
    # fallback: 発話冒頭を切り詰める
    fb = content.strip().replace("\n", " ")
    return fb[:max_chars]


def update_summary(
    old_summary: str,
    role: str,
    content: str,
    llm: LLMClient,
    model: str = SUMMARY_MODEL,
    max_chars: int = SUMMARY_MAX_CHARS,
) -> str:
    """既存 summary を新発話で更新.

    LLM 失敗時は old_summary をそのまま返す (= 失っても情報減らない).
    """
    if not content.strip():
        return old_summary or ""
    if not (old_summary or "").strip():
        return generate_initial_summary(role, content, llm, model, max_chars)
    prompt = _UPDATE_PROMPT_TEMPLATE.format(
        old_summary=old_summary[:600], role=role,
        content=content[:1000], max_chars=max_chars,
    )
    try:
        response = llm.generate(model, prompt, num_ctx=SUMMARY_NUM_CTX)
    except Exception:
        return old_summary
    cleaned = _clean(response, max_chars)
    return cleaned or old_summary
