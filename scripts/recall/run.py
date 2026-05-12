"""recall 主エントリ (UserPromptSubmit hook の同期処理から呼ばれる).

入力: 現発話 + DB
出力: additionalContext 文字列 (空文字 = 注入なし)

仕様書 §5 に従う:
- 5.1 LLM が発話を解析 (キーワード + 履歴参照意図)
- 5.5 検索結果を LLM が関連性 curate + 自然文要約してから main に渡す
"""
from __future__ import annotations

import os
import sqlite3

from scripts.debug.recall_log import (
    is_enabled as debug_enabled,
    log_final_prompt,
    log_hits,
    log_keywords,
)
from scripts.recall.extract import RECALL_MODEL, analyze_query
from scripts.recall.search import (
    bump_access_counts,
    search,
    search_episodes_by_embeddings,
    search_episodes_by_fts,
)
from scripts.recall.summarize import summarize_recall
from scripts.shared.ollama import LLMClient

EMBED_MODEL = os.environ.get("PERSONA_EMBED_MODEL", "nomic-embed-text")
BUFFER_N = int(os.environ.get("PERSONA_BUFFER_N", "3"))

# 0.6.7: recall 出力の total token budget (近似値). 0 = 無効化.
# 日本語 1 char ≒ 0.5-0.8 token, 英数記号 ≒ 0.25 token なので mixed で
# 1 char ≒ 0.4 token と粗く見積もる → chars_budget = budget / 0.4 = budget * 2.5.
# default 500 tokens は 6-8 fact + 数件 episode title 相当.
TOKEN_BUDGET = int(os.environ.get("PERSONA_RECALL_TOKEN_BUDGET", "500"))


def _estimate_tokens(text: str) -> int:
    """char 数からの粗い token 近似. 日本語混在前提で 1 token ≒ 2.5 char.

    tiktoken 等の正確な tokenizer を呼ばないのは:
    (a) 依存追加を避ける, (b) main agent 側の tokenizer が何かに左右される
    ため正確さに意味がない. 粗い ceiling として使えれば十分.
    """
    if not text:
        return 0
    return max(1, int(len(text) / 2.5))


def _truncate_to_budget(text: str, budget_tokens: int) -> str:
    """text 全体を token budget に収めるよう末尾切り捨て.

    行単位で末尾から落とす. 行が無くなったら char 単位で切り詰め.
    """
    if budget_tokens <= 0 or _estimate_tokens(text) <= budget_tokens:
        return text
    char_budget = int(budget_tokens * 2.5)
    lines = text.split("\n")
    while len(lines) > 1 and _estimate_tokens("\n".join(lines)) > budget_tokens:
        lines.pop()
    out = "\n".join(lines)
    if _estimate_tokens(out) > budget_tokens and char_budget > 0:
        out = out[:char_budget].rstrip() + "…"
    return out


def fetch_buffer(
    conn: sqlite3.Connection, n: int, session_id: str | None = None,
) -> list[dict]:
    """直前 N 発話を取得. session_id 指定時は同一 session に絞る (0.6.11).

    並行 session の発話が recall の文脈に混入して analyze_query が誤った
    keyword 抽出をするのを防ぐ. None なら全 session 横断 (後方互換).
    """
    if session_id is not None:
        rows = conn.execute(
            "SELECT role, content FROM episodes WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, n),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role, content FROM episodes ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def recall(
    conn: sqlite3.Connection,
    content: str,
    client: LLMClient,
    buffer_n: int = BUFFER_N,
    recall_model: str = RECALL_MODEL,
    embed_model: str = EMBED_MODEL,
    session_id: str | None = None,
) -> str:
    """ユーザー発話から関連記憶を引き、要約 additionalContext を返す。

    session_id 指定時は buffer 取得を同 session に絞る (0.6.11).
    facts / episodes の検索本体は全 session 横断のまま (session 跨ぎで
    過去知識を参照したいユースケースが多いため).
    """
    if not content.strip():
        return ""
    buffer = fetch_buffer(conn, buffer_n, session_id=session_id)

    # 1. LLM が発話を解析 (履歴参照意図 + keyword hint)
    #    keywords は debug / episode 検索 hint 用。recall を skip する判断には
    #    使わない (= 「意味のないやり取り」 という前提を持たない: ユーザーが
    #    『OK』『うん』『ありがとう』 を返した瞬間こそ、直前の議題を踏まえた
    #    応答が要る。発話の長短や形に関係なく毎発話 recall する)。
    analysis = analyze_query(content, buffer, client, model=recall_model)
    if debug_enabled():
        log_keywords(content, buffer, analysis.keywords)

    # 2. 発話全文を 1 回 embed する (短い個別 keyword を embed すると
    #    nomic-embed-text の OOV collapse で「MVP」「Phase 1」「猫」 等が
    #    全て同じ default embedding に化け、無関係な fact と cosine 距離 0
    #    で偽 hit する。発話全文なら長く・diverse でこの問題が起きない)。
    try:
        query_emb = client.embed(embed_model, content)
    except Exception:
        query_emb = []
    if not query_emb:
        if debug_enabled():
            log_final_prompt("")
        return ""
    embeddings: list[list[float]] = [query_emb]

    # 3. facts 検索
    hits = search(conn, embeddings)
    if debug_enabled():
        log_hits(len(analysis.keywords), [
            {
                "fact_id": h.fact_id, "category": h.category, "key": h.key,
                "distance": round(h.distance, 4), "importance": h.importance,
                "score": round(h.score, 4),
            }
            for h in hits
        ])

    # 4. episodes 検索 (LLM が「履歴参照」 と判断したとき)
    #    0.6.0 phase3 ハイブリッド: vec0 cosine (意味的近さ) + FTS5 BM25
    #    (短文 / 固有名詞 / typo の確実な hit) を union して
    #    nomic-embed-text の弁別力限界を物理的に塞ぐ.
    episodes_hits = []
    if analysis.search_history:
        vec_hits = search_episodes_by_embeddings(conn, embeddings)
        fts_hits = search_episodes_by_fts(conn, analysis.keywords or [])
        # union & dedup (episode_id ベース). vec hit を先に並べて FTS で補強
        seen_ids: set[int] = set()
        for h in vec_hits + fts_hits:
            if h.episode_id in seen_ids:
                continue
            seen_ids.add(h.episode_id)
            episodes_hits.append(h)

    if not hits and not episodes_hits:
        if debug_enabled():
            log_final_prompt("")
        return ""

    if hits:
        bump_access_counts(conn, [h.fact_id for h in hits])

    # 5. mode 別の出力生成 (0.6.5 で auto 判定を default 化)
    #    PERSONA_RECALL_MODE env で切替:
    #      auto         (default): 会話内容で動的判定. analysis.search_history が
    #                              True (= 過去参照系発話) なら index_titled,
    #                              False (= 雑談・新規話題) なら summarize.
    #      summarize             : 常に ローカル LLM で curate + 自然文要約
    #      index_titled          : 常に fact_id + value 先頭 30 字
    #      index_only            : 常に fact_id のみ (本文ゼロ, main agent に search 強制)
    #    auto は「過去参照では curate ミスを避けて main agent に手がかりを渡す、
    #    新規話題ではローカル curate で軽量に流す」 の動的切り替え.
    mode = os.environ.get("PERSONA_RECALL_MODE", "auto").strip().lower()
    if mode not in ("auto", "summarize", "index_titled", "index_only"):
        mode = "auto"
    if mode == "auto":
        mode = "index_titled" if analysis.search_history else "summarize"

    if mode == "summarize":
        summary = summarize_recall(content, hits, episodes_hits, client, model=recall_model)
        if not summary:
            if debug_enabled():
                log_final_prompt("")
            return ""
        additional_context = f"## 思い出した記憶\n{summary}"
    else:
        # index_* mode: ローカル LLM 不使用. hit を素直にインデックス化.
        additional_context = _format_index(hits, episodes_hits, mode=mode)
        if not additional_context:
            if debug_enabled():
                log_final_prompt("")
            return ""

    # 0.6.7: total token budget の ceiling. score 降順の前提で末尾から落とす.
    if TOKEN_BUDGET > 0:
        additional_context = _truncate_to_budget(additional_context, TOKEN_BUDGET)

    if debug_enabled():
        log_final_prompt(additional_context)
    return additional_context


def _format_index(hits, episodes_hits, mode: str = "index_titled") -> str:
    """ローカル LLM を通さず, hit を素直にインデックス化して main agent に渡す.

    mode='index_titled': fact_id + value 先頭 30 字 (= タイトル). main agent は
      タイトルから関連性を判断し、 詳細が要れば mcp__persona-memory__search_memory
      を呼んで本文取得する.
    mode='index_only': fact_id のみ. 本文ゼロで物理的に search 強制.

    どちらも ローカル LLM の curate ミスが構造的にゼロ. token も大幅削減.
    """
    title_chars = 30
    lines: list[str] = []
    if hits:
        lines.append("### 思い出しの手がかり (記憶)")
        for h in hits:
            if mode == "index_titled":
                title = (h.value or "")[:title_chars]
                lines.append(f"- #{h.fact_id} [{h.category}/{h.key}] {title}…")
            else:  # index_only
                lines.append(f"- #{h.fact_id}")
    if episodes_hits:
        lines.append("")
        lines.append("### 思い出しの手がかり (会話履歴)")
        for e in episodes_hits:
            if mode == "index_titled":
                title = (e.content or "")[:title_chars]
                lines.append(f"- ep#{e.episode_id} [{e.timestamp}] {e.role}: {title}…")
            else:
                lines.append(f"- ep#{e.episode_id}")
    if not lines:
        return ""
    note = (
        "\n\n_詳細が必要なら mcp__persona-memory__search_memory で深掘りしてください_"
    )
    return "## 思い出した記憶\n" + "\n".join(lines) + note
