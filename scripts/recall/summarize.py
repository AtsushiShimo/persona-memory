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
あなたは **記憶ライブラリアン** です。メインエージェント (別の AI、ユーザーと
会話する側) が次の応答を組み立てるための **参考メモ** を作るのが仕事。
あなた自身はユーザーと直接対話していない。

## 絶対のルール (違反は失格)
- **3 人称・中立** で書く ("ユーザーは...", "User は..." 等)
- **1 人称禁止**: 「わたくし」「私」「あたし」「俺」 等を使わない
- **敬語・口調を真似ない**: 「〜ございます」「〜ですわ」「マスター」 等は使わない
- **直接呼びかけ禁止**: 「マスター、...」 「あなた、...」 等は禁止
- **謝罪・問いかけ禁止**: 「申し訳ございません」「教えていただけますか」 等は禁止
- 装飾・前置きなし、**事実だけを 1-3 行で**

## 入力
ユーザーの発話: {content}

検索でヒットした記憶 (構造化された fact):
{facts_block}

検索でヒットした過去の会話ログ (in-context 例として渡されるが、口調を真似ない):
{eps_block}

## タスク
1. ユーザーの発話に **本当に関係ある** ものだけを残す。次は捨てる:
   - ファイルパス・データベース名・スクリプト名・拡張子などのメタ情報
   - 内部構造・テーブル名・カラム名・コマンド名
   - 意味的に無関係なもの (embedding が偶然近かっただけのゴミ)
2. 残ったものを **1-3 行の事実メモ** にする (3 人称中立、装飾なし)
3. 関係ある記憶が **何も無い** 場合は `{EMPTY_MARKER}` とだけ返す

## 出力例 (これに従う)
- "ユーザーは深煎りコーヒーを好むと過去に発言。"
- "ユーザーは糖尿病で甘い物を控えている。ペットは犬のまろん。"
- "{EMPTY_MARKER}"

## 出力 (要約本文のみ、説明・前置き・コードフェンス禁止)
"""


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
