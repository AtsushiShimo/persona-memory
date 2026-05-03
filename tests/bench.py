#!/usr/bin/env python3
"""Benchmark persona-memory vs obsidian-persona side-by-side.

Drives Claude Code in headless mode (`claude -p ...`) against each project
directory, runs each scenario as (setup turn → fresh session → probe turn),
captures the agent's text response and tool-call events, scores against
expected keywords / tool usage, and prints a comparison table.

Usage:
  python3 tests/bench.py                # run all scenarios
  python3 tests/bench.py --scenario 1   # run only scenario 1
  python3 tests/bench.py --side pm      # only persona-memory
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJ_PM = Path.home() / "Desktop/claude_dev/persona-memory-test2"
PROJ_OBS = Path.home() / "Desktop/claude_dev/obsidian-persona"

PROJECTS = {
    "pm": ("persona-memory", PROJ_PM),
    "obs": ("obsidian-persona", PROJ_OBS),
}

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
TURN_TIMEOUT = 300  # seconds

# LLM judge for semantic scoring (改善 1).
# Heavy model so the judgment understands negation / paraphrase / synonym.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
JUDGE_MODEL = os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b")
JUDGE_TIMEOUT = float(os.environ.get("PERSONA_BENCH_JUDGE_TIMEOUT", "60.0"))


@dataclass
class Scenario:
    id: int
    name: str
    setup: str
    probe: str
    keywords: list[str] = field(default_factory=list)
    # 自然文で書く「期待される事実」。LLM judge に渡され、応答テキストに
    # 意味的に含まれているかを判定する。空なら LLM judge をスキップ。
    expected_facts: list[str] = field(default_factory=list)
    require_tool: str | None = None  # name fragment of expected tool call
    forbid_tool: str | None = None


@dataclass
class TurnResult:
    text: str
    tool_calls: list[str]
    duration_s: float
    raw_event_count: int
    error: str | None = None


def run_turn(proj_dir: Path, prompt: str) -> TurnResult:
    """Run a single Claude Code turn in a *fresh* session."""
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=proj_dir, capture_output=True, text=True, timeout=TURN_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return TurnResult(text="", tool_calls=[], duration_s=TURN_TIMEOUT,
                          raw_event_count=0, error="timeout")
    duration = time.time() - t0

    events: list[dict[str, Any]] = []
    for line in proc.stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    text_parts: list[str] = []
    tool_calls: list[str] = []
    for e in events:
        et = e.get("type")
        if et == "result":
            r = e.get("result")
            if r:
                text_parts.append(r)
        elif et == "assistant":
            msg = e.get("message", {})
            for c in msg.get("content", []):
                if c.get("type") == "text":
                    text_parts.append(c.get("text", ""))
                elif c.get("type") == "tool_use":
                    tool_calls.append(c.get("name", "?"))

    text = "\n".join(p for p in text_parts if p)
    err = None
    if proc.returncode != 0:
        err = (proc.stderr or "")[:200]

    return TurnResult(
        text=text, tool_calls=tool_calls, duration_s=duration,
        raw_event_count=len(events), error=err,
    )


def score_keywords(text: str, keywords: list[str]) -> tuple[int, int, dict[str, bool]]:
    hits = {k: (k.lower() in text.lower()) for k in keywords}
    return sum(hits.values()), len(keywords), hits


def llm_judge(
    probe_text: str, expected_facts: list[str]
) -> tuple[int, int, dict[str, str]]:
    """gemma3:12b に「応答に各 fact が意味的に含まれているか」を判定させる。

    keyword include scoring が拾えない paraphrase / negation / synonym を
    semantic に評価する。yes / no / unsure の 3 値返答を JSON 配列で受ける。
    返り値: (yes 数, fact 総数, fact -> 判定 dict)。

    呼び出し失敗時は (0, 0, {}) を返し、SUMMARY 側で `llm=skip` 表示。
    """
    if not expected_facts:
        return (0, 0, {})
    try:
        import httpx
    except Exception:
        return (0, 0, {})

    facts_block = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(expected_facts))
    excerpt = probe_text.strip()
    if len(excerpt) > 4000:
        excerpt = excerpt[:4000] + "…[truncated]"

    prompt = (
        "以下のアシスタント応答に、期待される各事実が **functionally に含まれているか** を判定してください。\n"
        "ユーザーの理想は『端的に要点を押さえた答え』。**冗長さは加点要素ではない**。\n"
        "応答の長さ・elaboration の量・修飾語の有無は判定に無関係。**事実の核が伝わっていれば yes**。\n\n"
        "判定基準:\n"
        "- yes = 期待される事実の核が応答内で実際に伝わっている。短い言及・paraphrase・含意でも yes。\n"
        "  例: 期待「現職チーム規模は PM 含めて 8 人」、応答「チーム 8 人」→ **yes** (チーム規模の核は伝わる)\n"
        "  例: 期待「Java を避けたい」、応答「Java の方針確認したい」→ **yes** (避ける preference を拾って確認している)\n"
        "  例: 期待「auth NPE は V2.0.1 で解消」、応答「ホットフィックス投入済み」→ **yes** (解消の核は伝わる、版番号の明示は装飾)\n"
        "- no = 言及がない、または **反対の意味** で言っている。\n"
        "  例: 期待「Java を避けたい」、応答「Java で書きましょう」→ **no** (極性が逆)\n"
        "  例: 期待「ペットはさくら」、応答「ペットはみく」→ **no** (異なる事実)\n"
        "- unsure = 応答が短すぎる / 曖昧 / 判断材料が足りない (極端な場合のみ)。\n\n"
        "**重要原則**: 応答が長くて修飾語まで完璧に揃えていれば yes、短くて核だけ正しくても yes。\n"
        "短い正答を no にしないこと。冗長な elaboration を yes の必要条件にしないこと。\n\n"
        f"応答:\n{excerpt}\n\n"
        f"期待される事実 (各行 1 件):\n{facts_block}\n\n"
        "出力は JSON 配列のみ。例: [\"yes\", \"no\", \"unsure\"]\n"
        "前置き・後書き・コードフェンス禁止。配列の長さは事実数と一致させること。"
    )

    try:
        r = httpx.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": JUDGE_MODEL, "prompt": prompt, "stream": False},
            timeout=JUDGE_TIMEOUT,
        )
        r.raise_for_status()
        raw = (r.json().get("response") or "").strip()
    except Exception as e:
        print(f"    [llm_judge ERROR] {type(e).__name__}: {e}")
        return (0, 0, {})

    # コードフェンスや前後の文を緩く剥がす
    m = re.search(r"\[.*?\]", raw, re.DOTALL)
    arr_text = m.group(0) if m else raw
    try:
        verdicts = json.loads(arr_text)
    except json.JSONDecodeError:
        print(f"    [llm_judge PARSE FAIL] raw={raw[:200]!r}")
        return (0, 0, {})
    if not isinstance(verdicts, list) or len(verdicts) != len(expected_facts):
        return (0, 0, {})

    detail: dict[str, str] = {}
    yes = 0
    for fact, v in zip(expected_facts, verdicts):
        v_norm = str(v).strip().lower()
        if v_norm not in ("yes", "no", "unsure"):
            v_norm = "unsure"
        detail[fact] = v_norm
        if v_norm == "yes":
            yes += 1
    return (yes, len(expected_facts), detail)


def score_tool(tool_calls: list[str], require: str | None, forbid: str | None) -> str:
    if require:
        if any(require.lower() in t.lower() for t in tool_calls):
            return f"OK (used {require})"
        return f"FAIL (did not call {require})"
    if forbid:
        if any(forbid.lower() in t.lower() for t in tool_calls):
            return f"FAIL (called {forbid})"
        return f"OK (did not call {forbid})"
    return "(no tool requirement)"


SCENARIOS: list[Scenario] = [
    Scenario(
        id=1,
        name="ユーザー属性 + 振る舞い好み",
        setup=(
            "自己紹介します。バックエンドエンジニアの山田太郎 (ヤマダタロウ) です。"
            "メイン言語は Rust と Python、機械は M3 Mac。最近黒猫を保護して、"
            "名前は「みく」、3 歳のメスです。\n\n"
            "それと、コードレビューは率直に指摘してほしい派です。"
            "「良いと思います」みたいな曖昧な返答はやめて、遠慮なく指摘してください。"
        ),
        probe="私のメイン言語って何でしたっけ？ペットの名前と、コードレビューの好みも教えて。",
        keywords=["Rust", "Python", "みく", "率直"],
        expected_facts=[
            "ユーザーのメイン言語は Rust と Python である",
            "ユーザーのペットの名前は「みく」である",
            "ユーザーはコードレビューで率直な指摘 (曖昧な返答ではない) を求めている",
        ],
    ),
    Scenario(
        id=2,
        name="一時的指示の非永続化 (今日だけ)",
        setup=(
            "今日のセッションだけ、コードブロックは Markdown じゃなくプレーンテキストで"
            "返してください。後でメール貼り付けに使うので。"
            "**これは今日のためだけの話**で、明日以降のセッションでは普通に Markdown "
            "コードブロックで返して OK。"
        ),
        probe="簡単な Hello World の Python コードを書いて",
        # 期待: 過去の「プレーンテキスト」一時指示を引き継がず、デフォルトの Markdown
        # コードブロック (```python ... ```) で返す
        keywords=["```"],
        expected_facts=[
            "Python コードが Markdown コードブロック形式 (```) で示されている",
            "(過去の『今日だけプレーン』という一時指示が永続化されていない)",
        ],
    ),
    Scenario(
        id=9,
        name="メタルール compliance (AskUserQuestion)",
        setup=(
            "ちょっとルール追加させて。あなたが私に質問する時、選択肢で答えられる類の"
            "質問は必ず AskUserQuestion ツールを使ってください。プレーンテキストで"
            "「A と B どちらにしますか？」みたいに聞くのは禁止です。"
            "理由は AskUserQuestion ならクリックで答えられて速いから。\n\n"
            "了解したら「了解」と返してください。"
        ),
        probe="新しい依存ライブラリを入れたいんだけど、どれが良いか相談に乗って",
        require_tool="AskUserQuestion",
    ),
    Scenario(
        id=3,
        name="進行中タスク (Kafka→Pulsar 移行)",
        setup=(
            "いま会社で Kafka から Pulsar への移行プロジェクトを進めてる。"
            "理由は Kafka の運用コストが高いから。今週中に PoC 環境を立てて、"
            "来週から本番のトラフィックの 5% をシャドウ送信する計画。"
            "担当は私を含めて 3 人。\n\n"
            "PoC 環境のスキーマレジストリどうする？Confluent Schema Registry を "
            "Pulsar 側に移すのが面倒で。"
        ),
        probe="昨日の続きやろうか",
        keywords=["Kafka", "Pulsar", "PoC", "5%", "スキーマ"],
        expected_facts=[
            "ユーザーは Kafka から Pulsar への移行プロジェクトを進めている",
            "PoC 環境のスキーマレジストリの扱い (Confluent SR の移行) が論点になっている",
            "来週から本番トラフィックの 5% をシャドウ送信する計画がある",
        ],
    ),
    Scenario(
        id=4,
        name="固有名詞・略語の保持",
        setup=(
            "うちのプロダクトは『Mizuki』という名前のセルフホスト型ベクトル DB。"
            "Mizuki は Qdrant と pgvector のハイブリッド構成で、月間 10 億ベクトルを扱う。"
            "チーム内では『M』って略して呼ぶこともある。"
        ),
        probe="M の月間ベクトル数って何だっけ？",
        # 期待: 「M」 = 「Mizuki」 と解決して 10 億ベクトルと答える
        keywords=["Mizuki", "10 億"],
        expected_facts=[
            "M は Mizuki の略称である",
            "Mizuki は月間 10 億ベクトルを扱う",
            "Mizuki はセルフホスト型のベクトル DB である",
        ],
    ),
    Scenario(
        id=5,
        name="矛盾・更新 (英→日コミットメッセージ好み変更)",
        setup=(
            "コミットメッセージは英語派です。日本語コードベースでも英語で書いてる。"
            "\n\n... と前回までは思ってたんだけど、最近気持ちが変わって"
            "コミットメッセージは日本語に統一したい。チームメンバーへの可読性を"
            "優先することにしました。"
        ),
        probe="新しい PR 用のコミットメッセージのドラフトを書いて",
        # 期待: 日本語コミットメッセージ。英語だけだと ✗
        keywords=["日本語"],
        expected_facts=[
            "コミットメッセージは日本語で書く方針 (現時点で有効な好み)",
            "(かつての英語派の好みは撤回されており、もう適用されない)",
        ],
    ),
    Scenario(
        id=6,
        name="人間関係 (役職とスタンス)",
        setup=(
            "チームメンバー紹介。CTO の佐藤さんは技術判断の最終決定者で、"
            "新技術の導入には保守的なタイプ。VP of Eng の鈴木さんは実行重視で、"
            "CTO の事前承認を取らずに動くこともある。私 (山田) は両者の間で"
            "調整役として動いてる。"
        ),
        probe="VP of Eng って誰だっけ？性格も教えて。",
        keywords=["鈴木", "実行"],
        expected_facts=[
            "VP of Eng は鈴木さんである",
            "鈴木さんは実行重視で、CTO の事前承認を取らずに動くこともある",
        ],
    ),
    Scenario(
        id=7,
        name="ドメイン情報 (本番 DB 構成)",
        setup=(
            "うちの本番 DB は Aurora MySQL 8.0.34、リードレプリカが 3 つ、"
            "最大接続数は 500。staging は同じバージョンの RDS シングルインスタンス。"
        ),
        probe="本番 DB のスペックって何だっけ？",
        keywords=["Aurora", "MySQL", "8.0.34", "リードレプリカ", "500"],
        expected_facts=[
            "本番 DB は Aurora MySQL 8.0.34",
            "リードレプリカは 3 つ",
            "最大接続数は 500",
        ],
    ),
    Scenario(
        id=8,
        name="学習進捗・状態の度合い",
        setup=(
            "Rust の async / await を勉強中なんだけど、tokio の Pin と Send 周りで"
            "詰まってる。実装は書けるが、なぜそうなってるのかが腹落ちしてない。"
            "先週から手を付けて 2 週間目で、進捗としてはまだ半分くらい。"
        ),
        probe="私の Rust 学習どこまで進んでた？",
        keywords=["tokio", "Pin", "詰ま", "半分"],
        expected_facts=[
            "ユーザーは Rust の async / await を学習中で、tokio の Pin と Send 周りで詰まっている",
            "実装は書けるが原理面が腹落ちしていない",
            "学習開始から 2 週間目で進捗は半分程度",
        ],
    ),
    # ─── 意地悪系 ───────────────────────────────────────
    Scenario(
        id=10,
        name="ネガーション (やりたくないこと)",
        setup=(
            "ちょっと申し送り。私の Java 経験は 2 年あるけど、基本的に Java は"
            "書きたくないんだよね。レビューも他の人に頼みたいタイプ。"
            "好きな言語と苦手な言語を分けて覚えてほしい。"
            "あと TypeScript は strict mode 必須派。`as never` みたいな"
            "型キャストで誤魔化すコードには厳しい目線で。"
        ),
        probe="新しい Java のマイクロサービスのレビュー、お願いできる？",
        # 期待: 「Java は避けたい派」と認識して断る or 別の人を勧める
        keywords=["Java", "避け", "苦手"],  # 「避けたい」「苦手」のニュアンスがあれば OK
        expected_facts=[
            "ユーザーは Java を書きたくない / 避けたいと表明している",
            "Java のレビューは別の人 / 他の人に依頼するのが望ましい",
            "アシスタントは Java レビュー依頼を即座に引き受けず、確認 / 委託の意思を示している",
        ],
    ),
    Scenario(
        id=11,
        name="数字てんこもり (SLO / インフラ)",
        setup=(
            "うちの公開 API のスペック詳細メモするね。SLA は 99.5% (月 3.6h ダウン許容)、"
            "レイテンシ目標 P99 200ms / P50 50ms、QPS ピーク 5000、月間リクエスト 200M、"
            "CDN は CloudFront (US/JP/EU エッジ)、データセンター ap-northeast-1 と "
            "us-east-1 の active-active 構成、CDN cache TTL は GET で 60 秒、"
            "認証付きパスは no-cache。"
        ),
        probe="うちの API のスペックスペック (SLA / レイテンシ / トラフィック / インフラ) もう一度教えて",
        keywords=["99.5", "200ms", "5000", "CloudFront", "ap-northeast-1", "active-active"],
        expected_facts=[
            "SLA は 99.5%",
            "P99 レイテンシ目標は 200ms",
            "ピーク QPS は 5000",
            "CDN は CloudFront",
            "ap-northeast-1 と us-east-1 の active-active 構成",
        ],
    ),
    Scenario(
        id=12,
        name="時系列 + 状態遷移",
        setup=(
            "先週の月曜にリリースした V2 API、火曜にバグ判明 (auth エンドポイントで"
            " NPE 大量発生、新規ログイン全滅)、水曜に hotfix V2.0.1 出して収束した。"
            "今週月曜から V3 の設計を始めていて、来週金曜までに RFC ドラフトを"
            "書きたい。レビュアーは @tanaka と @suzuki。"
        ),
        probe="auth の NPE 問題、もう直った？V3 のスケジュールも教えて。",
        keywords=["V2.0.1", "hotfix", "V3", "RFC", "来週金曜"],
        expected_facts=[
            "V2 API の auth NPE は hotfix V2.0.1 で解消済み",
            "V3 の RFC ドラフトを来週金曜までに書く予定",
            "V3 のレビュアーは @tanaka と @suzuki",
        ],
    ),
    Scenario(
        id=13,
        name="長文プロフィール (深い埋もれ)",
        setup=(
            "ちょっと自己紹介させて。色々混じってるけど覚えてくれると嬉しい。\n\n"
            "私は東京出身、35歳、バックエンドエンジニア歴 12 年。前職はメガベンチャーで"
            "決済システム (年間流通 1.2 兆円スケール) を担当、PCI-DSS 準拠の card vault"
            "の実装と運用も。AWS と GCP 両方触るけど、最近は GCP 中心。"
            "チームは PM 含めて 8 人、私はテックリード。\n\n"
            "プライベートは、妻 (デザイナー) と娘 (3 歳) と一緒に暮らしてる。"
            "趣味はランニング、週 30km 目安。最近サブ 3 (フルマラソン 3 時間切り) を"
            "狙ってる。今シーズンの自己ベストは 3 時間 12 分。\n\n"
            "技術的には、Python 12 年、Go 7 年、最近 Rust を始めて半年。"
            "Java は前職で 5 年触ったけど避けたい派。エディタは Neovim、"
            "tabs ではなく spaces (4 個)、行幅 100 文字。コミットメッセージは英語。"
        ),
        probe=(
            "ちょっと教えて: 私のエンジニア歴と、Rust 歴はそれぞれ何年？あと"
            "テックリードしてる現職のチーム規模は？"
        ),
        keywords=["12", "半年", "8"],  # 12年, Rust半年, 8人
        expected_facts=[
            "ユーザーのバックエンドエンジニア歴は 12 年",
            "Rust 歴は半年 (始めて 6 ヶ月程度)",
            "現職のチーム規模は PM 含めて 8 人",
        ],
    ),
    Scenario(
        id=14,
        name="意図的な情報訂正 (古い情報の打ち消し)",
        setup=(
            "あ、ごめん、前にうちのペットを「黒猫のみく、3 歳メス」って言ったの、"
            "間違いだった。実は「白猫のさくら、5 歳メス」が正しい。みくは別の友達の"
            "猫の名前と混同してた。さくらの方をペットとして覚えて、みくは忘れて。"
        ),
        probe="うちのペットの名前と特徴、教えて",
        keywords=["さくら", "白猫", "5"],
        # 古い「みく」「黒猫」「3 歳」が混じっていたら部分的減点 (要 manual check)
        expected_facts=[
            "ペットは白猫の「さくら」、5 歳メス",
            "(以前のメモ「黒猫みく 3 歳」は誤りとして取り消されている)",
        ],
    ),
]


def run_scenario(side_label: str, proj_dir: Path, sc: Scenario) -> dict[str, Any]:
    print(f"  [setup] sending {len(sc.setup)} chars...", flush=True)
    r1 = run_turn(proj_dir, sc.setup)
    print(f"    text: {r1.text[:80]!r}{'...' if len(r1.text) > 80 else ''}")
    print(f"    tools: {r1.tool_calls}")
    print(f"    duration: {r1.duration_s:.1f}s")
    if r1.error:
        print(f"    [ERROR] {r1.error}")

    # Give Stop hook a moment to finalize DB writes
    time.sleep(2)

    print(f"  [probe] sending fresh-session...", flush=True)
    r2 = run_turn(proj_dir, sc.probe)
    print(f"    text: {r2.text[:200]!r}{'...' if len(r2.text) > 200 else ''}")
    print(f"    tools: {r2.tool_calls}")
    print(f"    duration: {r2.duration_s:.1f}s")
    if r2.error:
        print(f"    [ERROR] {r2.error}")

    kw_hits, kw_total, kw_detail = score_keywords(r2.text, sc.keywords)
    tool_score = score_tool(r2.tool_calls, sc.require_tool, sc.forbid_tool)
    llm_yes, llm_total, llm_detail = llm_judge(r2.text, sc.expected_facts)

    return {
        "side": side_label,
        "scenario": sc.id,
        "setup_duration": r1.duration_s,
        "probe_duration": r2.duration_s,
        "probe_text": r2.text,
        "probe_tools": r2.tool_calls,
        "kw_hits": kw_hits,
        "kw_total": kw_total,
        "kw_detail": kw_detail,
        "llm_yes": llm_yes,
        "llm_total": llm_total,
        "llm_detail": llm_detail,
        "tool_score": tool_score,
        "errors": [e for e in (r1.error, r2.error) if e],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", type=int, default=None,
                    help="run only this scenario id")
    ap.add_argument("--side", choices=list(PROJECTS), default=None,
                    help="run only this side (pm|obs)")
    args = ap.parse_args()

    sides = [args.side] if args.side else list(PROJECTS)
    scenarios = (
        [s for s in SCENARIOS if s.id == args.scenario]
        if args.scenario else SCENARIOS
    )
    if not scenarios:
        print(f"no scenarios match id={args.scenario}", file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = []
    for sc in scenarios:
        print(f"\n========== Scenario {sc.id}: {sc.name} ==========")
        for side_key in sides:
            label, pdir = PROJECTS[side_key]
            print(f"\n--- {label} ({pdir.name}) ---")
            if not pdir.exists():
                print(f"  [SKIP] {pdir} does not exist")
                continue
            r = run_scenario(label, pdir, sc)
            results.append(r)

    # Summary
    print("\n\n========== SUMMARY ==========")
    by_scenario: dict[int, list[dict[str, Any]]] = {}
    for r in results:
        by_scenario.setdefault(r["scenario"], []).append(r)
    for sid, rs in by_scenario.items():
        print(f"\n--- Scenario {sid} ---")
        for r in rs:
            kw = f"{r['kw_hits']}/{r['kw_total']}"
            llm = (
                f"{r['llm_yes']}/{r['llm_total']}"
                if r["llm_total"] > 0
                else "skip"
            )
            # DIVERGE: keyword と LLM judge の正答率が 0.3 以上ずれた場合に強調。
            # keyword の粗さ (paraphrase / negation の取りこぼし) を可視化する。
            diverge = ""
            if r["kw_total"] > 0 and r["llm_total"] > 0:
                kw_rate = r["kw_hits"] / r["kw_total"]
                llm_rate = r["llm_yes"] / r["llm_total"]
                if abs(kw_rate - llm_rate) >= 0.3:
                    diverge = " [DIVERGE]"
            print(
                f"  {r['side']:25} kw={kw} llm={llm}{diverge} "
                f"tool={r['tool_score']} "
                f"setup={r['setup_duration']:.0f}s probe={r['probe_duration']:.0f}s"
            )
            if r["errors"]:
                print(f"    errors: {r['errors']}")

    # Save raw
    out = Path("/tmp/bench_results.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\nraw results saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
