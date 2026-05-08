"""recall LLM による検索結果の **関連性 curate + 要約** (仕様書 §5.5).

検索でヒットした facts / episodes を生のまま main agent に渡すのではなく、
ローカル LLM が:
- ユーザー発話との関連性を判断 (無関係なファイルパス・内部メタ情報など
  は捨てる)
- 残ったものを 2-5 行の自然文に要約

これにより、score 上位だけど無関係な fact (例: `context/凜.db = 凜のデータ
ベースファイル`) が main agent に流出するのを防ぐ。
"""
from __future__ import annotations

from scripts.recall.extract import RECALL_MODEL
from scripts.recall.search import RecalledEpisode, RecalledFact
from scripts.shared.ollama import LLMClient

EMPTY_MARKER = "(該当なし)"


def _format_fact(f: RecalledFact) -> str:
    return f"- [{f.category}/{f.key}] {f.value} (importance={f.importance})"


def _format_episode(e: RecalledEpisode) -> str:
    return f"- [{e.timestamp}] [{e.role}] {e.content}"


def build_summarize_prompt(
    content: str,
    facts: list[RecalledFact],
    episodes: list[RecalledEpisode],
) -> str:
    facts_block = "\n".join(_format_fact(f) for f in facts) if facts else "(なし)"
    eps_block = "\n".join(_format_episode(e) for e in episodes) if episodes else "(なし)"
    return f"""\
記憶検索結果を関連性でフィルタして整形するツール。
出力は別 AI への入力データ。対話・口調・人格は不要、データだけ。

[ユーザーの問い]
{content}

[ヒットした記憶 fact]
{facts_block}

[ヒットした会話ログ]
{eps_block}

指示:
- **問いに対する直接的な答えになりうるもの** を最優先で残す
  例: 問い「実装優先度どうだったっけ?」 → Phase 分け / 優先順位を述べた fact や log
  例: 問い「ペット飼ってたよね?」 → ペット名 / 種類 / 性別を述べた fact や log
- 関連が薄くても問いの主題と関係する補足情報は残してよい (importance 表示は捨てて自然文で)
- ファイル名・パス・テーブル名・コマンド名・embedding 偶然マッチは捨てる
- ヒット数が多くても、問いに関連する 3-7 件に絞る
- 残ったものを markdown 箇条書きで列挙 (`- <内容>`)
- 関連が皆無なら `{EMPTY_MARKER}` のみ

出力 (整形結果のみ、説明・前置き・コードフェンス禁止):"""


def summarize_recall(
    content: str,
    facts: list[RecalledFact],
    episodes: list[RecalledEpisode],
    client: LLMClient,
    model: str = RECALL_MODEL,
) -> str:
    """検索結果を LLM で関連性 curate + 要約する。

    戻り値:
    - 要約された自然文 (空文字 = main に渡すべき関連記憶なし)
    """
    if not facts and not episodes:
        return ""
    from scripts.debug.recall_log import _emit, is_enabled as _debug_enabled
    prompt = build_summarize_prompt(content, facts, episodes)
    if _debug_enabled():
        _emit("recall.summarize.prompt", "c", {"prompt": prompt, "length": len(prompt)})
    try:
        response = client.generate(model, prompt)
    except Exception as e:
        if _debug_enabled():
            _emit("recall.summarize.response", "c", {"error": str(e)})
        return ""
    if _debug_enabled():
        _emit(
            "recall.summarize.response", "c",
            {"response": response, "length": len(response)},
        )
    summary = response.strip()
    if not summary or summary == EMPTY_MARKER:
        return ""
    return summary
