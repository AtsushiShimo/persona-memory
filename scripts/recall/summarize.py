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
あなたはメインエージェント (人格を持つ AI パートナー) のために、検索でヒットした
記憶を **本当に関係あるものだけ残して要約** するアシスタントです。

ユーザーの発話:
{content}

検索でヒットした記憶 (構造化された fact):
{facts_block}

検索でヒットした過去の会話ログ:
{eps_block}

タスク:
1. ユーザーの発話に **本当に関係ある** ものだけを残す。次のものは捨てる:
   - ファイルパス・データベースファイル名・スクリプト名・拡張子などのメタ情報
   - プラグイン内部構造・テーブル名・カラム名・コマンド名
   - ユーザーの発話と意味的に無関係なもの (キーワード embedding が偶然
     近かっただけで、人間が読んで「関係ない」 と感じるもの)
2. 残ったものを **2-5 行の自然な日本語** で要約する
   - 「思い出しながら話す」 体で、メインエージェントがそのまま会話に
     織り込めるトーン
   - 構造化記法 (- list / [category/key]) は使わず、人間の記憶のような
     流れる文章で
3. ユーザーの発話と関係ある記憶が **何も無い** 場合は、`{EMPTY_MARKER}`
   とだけ返す

出力は要約本文のみ (説明・前置き・コードフェンス禁止):"""


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
