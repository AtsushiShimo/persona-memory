"""write LLM による fact 抽出。

input: 発話 (role + content) + 直近 BUFFER_N 発話
output: list[FactCandidate]

LLM は JSON 配列で抽出結果を返す。parse 失敗 / 空 → []。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from scripts.shared.ollama import LLMClient

# write の default backend / model.
# 0.5.13 で default を heavy (gemma3:12b) に上げた。light (gemma3:4b) では
# fact extraction の精度 (key/value 整合 / 既存値の上書き判定 / 矛盾検知) が
# 不足する事例が積み重なったため。env で従来動作 (light) や Claude 切り替え
# 可能 (= モデル性能 vs プロンプト/DB 設計 の切り分け診断用):
#   PERSONA_WRITE_BACKEND=ollama  (default)
#   PERSONA_WRITE_BACKEND=claude  (`claude -p` 子プロセスに丸投げ)
#   PERSONA_WRITE_MODEL=<model>   (ollama backend のみ。default gemma3:12b)
WRITE_BACKEND = os.environ.get("PERSONA_WRITE_BACKEND", "ollama").strip().lower()
WRITE_MODEL = os.environ.get(
    "PERSONA_WRITE_MODEL",
    os.environ.get("PERSONA_HEAVY_MODEL", "gemma3:12b"),
)

# write prompt は buffer 10 turn + テンプレートで 6-12k token 想定. KV cache を絞る.
WRITE_NUM_CTX = int(os.environ.get("PERSONA_WRITE_NUM_CTX", "16384"))

VALID_CATEGORIES = (
    "persona", "rule", "preference", "aversion", "profile", "skill", "context",
    "knowledge",
)


@dataclass
class FactCandidate:
    category: str
    key: str
    value: str
    importance: int
    # 0.6.16 反事実記憶 Phase A2: この fact が既存の言質を「撤回・変更」 する場合、
    # その理由を 1 文で. None なら撤回ではなく純粋な新規 / 補強.
    # apply_candidate が supersede 判定した時のみ persist 経路で使われる。
    reason: str | None = None

    def is_valid(self) -> bool:
        return (
            self.category in VALID_CATEGORIES
            and bool(self.key)
            and bool(self.value)
            and 1 <= self.importance <= 9
        )


@dataclass
class NodeCandidate:
    """議論グラフ用ノード候補 (0.6.17 案 1 Phase A2).

    write LLM の同一呼び出しで facts と一緒に抽出される. graph.add_node に
    渡して discussion_nodes テーブルへ保存する素材.
    """
    kind: str       # topic / option / decision / retraction / rationale
    title: str
    state: str = "proposed"  # proposed / accepted / rejected / superseded / observed
    content: str | None = None

    def is_valid(self) -> bool:
        from scripts.discussion.graph import VALID_KINDS, VALID_STATES
        return (
            self.kind in VALID_KINDS
            and self.state in VALID_STATES
            and bool(self.title)
        )


_PROMPT_TEMPLATE = """\
ユーザーとアシスタントの会話から、長期記憶として残すべき事実 (fact) を抽出するツール。

## category と key の対応
- **persona**: ユーザーがエージェントに求める振る舞い (例: 「率直に話して」)
- **rule**: 守るべきハード制約 (例: 「main に force push 禁止」)
- **preference**: ユーザーの個人的な好み (例: コーヒーは深煎り)
- **aversion**: ユーザーが避けたいもの (例: Java は書きたくない)
- **profile**: ユーザーの属性・家族・ペット・健康など (例: 糖尿病、ペットの名前)
- **skill**: 技術知識・経験 (本人経験 + 調査で得た知識の両方)
- **context**: 進行中のプロジェクト・調査結論・決定事項

## key/value の品質ルール (絶対遵守)

1. **key は category と value の両方に整合させる**:
   - 良い例: `category=profile, key=health_diabetes, value=糖尿病があり甘い物を控えている`
   - 悪い例: `category=profile, key=dog_maron, value=糖尿病があり…` (key=犬なのに value=健康、不一致)
   - 悪い例: `category=rule, key=deep_research_recommendation, value=…推奨` (推奨は rule じゃなく context)
2. **1 fact = 1 属性 / 出力では key の重複禁止**:
   1 つの fact は 1 つの属性のみを表す. 1 発話に複数の独立した属性がある場合は
   **必ず異なる key で別々の fact** に分けて出力する.
   **同じ key で複数の fact を出すのは絶対禁止** (= 後段で互いに上書きされて情報が失われる).

   - ✅ 発話「愛犬の名前はまろん、ミニチュアダックスフンド、オス」
     ```
     [{{"category": "profile", "key": "pet_dog_name",   "value": "まろん", "importance": 7}},
      {{"category": "profile", "key": "pet_dog_breed",  "value": "ミニチュアダックスフンド", "importance": 6}},
      {{"category": "profile", "key": "pet_dog_gender", "value": "オス", "importance": 6}}]
     ```
   - ❌ 同発話で 3 fact 全部 key="pet_dog_name" にする (上書きされて 1 件しか残らない)
   - ❌ 1 fact `key=pet_dog_name, value="まろん、ミニチュアダックスフンド、オス"` (詰め込み禁止)

   - ✅ 発話「コーヒーは深煎り派、砂糖なし」
     ```
     [{{"category": "preference", "key": "coffee_roast", "value": "深煎り", "importance": 5}},
      {{"category": "preference", "key": "coffee_sugar", "value": "入れない", "importance": 5}}]
     ```
   - ❌ 1 fact `key=coffee_preference, value="深煎り派、砂糖なし"`

   理由: 後で属性ごとに独立に更新可能 (= 浅煎りに変えても砂糖の話が消えない).
3. **key は snake_case 英数字、属性を表す**:
   良い key: `coffee_roast`, `coffee_sugar`, `coffee_milk`, `pet_dog_name`,
   `pet_dog_breed`, `pet_dog_gender`, `health_diabetes`, `phase1_scope` 等.
   1 ジャンル内で属性を細かく分ける (例: `coffee_*` で複数の独立属性 key を作る).
4. **value は短い事実形式のみ**:
   - 良い: `深煎り派` / `まろん` / `糖尿病` / `Phase 1 = MVP で進める`
   - 悪い: `深煎りが好みだが、今回の発言で浅煎りの方が好きだと判明` (経緯/説明文化)
   - 悪い: `ユーザーは犬を飼っており、その名前はまろんで…` (主語と前置きを書かない)
   - 経緯・推測・主語・前置き・「と判明」「と思われる」 等は禁止.
   - **生発話そのままの主旨だけを短く** 書き写す感覚.
5. **同じ value を category 違いで複数回 fact 化しない**

## 抽出対象 (= **今回の発話で新たに述べられた事実 / 確定した決定** のみ)

(a) ユーザーが述べた個人的事実 → preference / aversion / profile / skill / context
(b) ユーザーがエージェントに求める振る舞い → persona
(c) ユーザーが守らせたいハード制約 → rule
(d) ツール結果で **確認済みの** 外部知識 → 主に skill / context
(e) 会話で **確定した** 調査結論・実装方針・決定事項 → 主に context
(f) **状況依存のノウハウ / 教訓** (= 条件付き行動指針) → `persona` カテゴリ +
   key 先頭に `playbook_` (詳細は下の「playbook 抽出ルール」 参照)

**user / assistant どちらの発話からでも抽出する**: アシスタントの応答内で
合意・採用案・結論・実装方針が明示された場合も同様に context fact 化する.
ただし「アシスタントの推測 / 仮説 / 提案段階」 は除外 — 確定 signal
(ユーザーの「OK」「採用」 等 or アシスタント自身の「〜で確定です」「決定」 等
明確な確定表現) を伴うもののみ抽出.

## playbook 抽出ルール (条件付きノウハウ)

ユーザーが **「次回似た状況ではこう動いてほしい」** と教えた / ユーザーの
指摘で得た学び / 失敗パターンと対処を、 **状況→行動** のマップとして保存する.

**抽出条件 (どれか満たす)**:
- ユーザーが「〜の時は〜しろ」「次回は〜して」 と明示的に教えた
- 失敗 → ユーザーの指摘 → 修正 の流れで学んだ汎用則
- 「こういう時はこう動くべき」 の一般化された指針

**保存形式**:
- category: `persona`
- key: `playbook_<状況の英語スラッグ>` (例: `playbook_external_tool_failure`)
- value: 自然語で「**トリガー: 〜 / 行動: 〜**」 を **両方含める**.
  トリガー部分は **具体ツール名 / 具体キーワード** を列挙して、 後でユーザー
  発話と embedding 検索で hit しやすくする.
- importance: 7-8 (recall 優先度高め)

**良い例**:
```
{{"category": "persona", "key": "playbook_external_tool_failure",
  "value": "トリガー: Stitch / Figma / 外部 SaaS / MCP ツールが繋がらない・"
           "うまく動かない・接続エラー・連携失敗 等. "
           "行動: ユーザーに状況を聞き返す前に、 まず公式の不具合情報・"
           "Service Status・既知 Issue を Web 検索して現在の障害有無を確認する.",
  "importance": 8}}
```

**悪い例**:
- value="外部ツールエラー時は検索する" ← トリガーキーワードが少なくて hit しづらい
- value="Stitch エラー時は検索" ← Stitch 専用で他ツールに転用できない (汎用則として書け)
- key=`how_to_handle_errors` ← `playbook_` prefix が無い (= dynamic recall で扱われない)

**重要 (絶対遵守)**:
- **直近の会話 (buffer) は文脈理解のためだけに参照する**.
  buffer に書かれた過去の事実そのものを再 fact 化してはいけない.
  例: buffer に「コーヒーは深煎り派」 (1 ターン前)、 今回の発話「やっぱり浅煎り
  が好き」 → 抽出するのは `coffee_roast=浅煎り` のみ. 「深煎り」 は再生成しない.
- **自己矛盾する fact を同 batch で出さない**.
  例: 発話「牛乳は入れずにブラックで」 → 抽出は `coffee_milk=入れない` のみ.
  `coffee_milk=入れる` と `coffee_milk=入れない` の両方を出さない.
- **訂正・撤回された情報は新しい value で 1 fact のみ**.
  「やっぱり X じゃなく Y」 → `key=Y` の 1 fact だけ. X の fact は出さない
  (= 後段 supersede で旧版が history に残る).

## 決定検知 (重要: 多ターン議論の決定を即時 fact 化)

直近の会話を **広い window で見て**、以下のような **「直前数ターンで議論されていて、
今回の発話で確定した決定」** を context カテゴリで即時 fact 化する:
- ユーザーが選択肢に対して「Aで」「採用」「OK」「決定」「進める」「了解」 等で確定
- 「保留」「却下」「廃案」 等で明示的に外した項目
- 議論を経て合意された方針・優先順位・採用機能・採用しない機能

**value 冒頭に議論の主題語を必ず含める** (検索ヒット率を物理的に上げるため):
- ✅ value="**Renju の UI**: デフォルトカテゴリ名は「全体」、 デフォルトトピック名は「全体トピック」 で確定"
- ❌ value="デフォルトカテゴリ名は「全体」"   ← 「Renju」 で検索しても主題が無く hit 困難
- ✅ value="**Phase 1 (MVP)**: X / Y / Z を含む、 W は Phase 2 へ"
- ❌ value="X / Y / Z を含む"                  ← 何の Phase の話か不明

主題語が直近会話に明示されていれば必ず冒頭に置く. プロジェクト名 / 機能名 /
固有名詞 / ジャンル名のいずれか. これにより全文検索 (FTS5) でもベクター検索
でも確実に hit する.

**1 決定 = 1 fact が原則**。 複数論点が同時に確定したら **論点ごとに別 fact** に分けて出す:
- ✅ 「カテゴリ命名 = 全体、 トピック命名 = 全体トピック、 削除不可・リネーム可」 →
  3 fact (`renju_default_category_name`, `renju_default_topic_name`,
  `renju_default_category_topic_mutability`) に分割
- ❌ 1 fact で全部詰め込む (= 後で個別更新できず、 supersede で全部失う)

key 命名例 (主題 prefix + 属性):
- `renju_default_category_name`  value="Renju の UI: デフォルトカテゴリ名は「全体」 で確定"
- `phase1_scope`                  value="Phase 1 (MVP): X / Y / Z を含む、 W は Phase 2 へ"
- `feature_X_decision`            value="機能 X は採用、 Y は廃案 (議論主題: Z プロジェクトの UI 仕様)"
- `priority_order`                value="実装優先度: A > B > C で確定 (議論主題: バックエンド refactor)"

決定の根拠が直近会話バッファに無い (= 文脈不足) なら抽出しない。
推測ではなく **確定の signal** が直前 1-2 ターンで明示されているもののみ。

## 除外対象
- アシスタントの推測・仮説 (「〜かもしれない」「可能性がある」)
- 単なる質問・提案・選択肢提示
- ツール結果でも未検証 / 未確認のもの
- 雑談・繰り返し・既に DB にある情報の再確認
- ファイル名・パス・ツール名・モデル名・テーブル名等のメタ情報
- プラグイン本体の議論 (commit/リリース/bug fix/recall などの内部用語)

## 出力形式
- importance: 1-9
  - persona / rule: 8-9
  - profile / skill (本人経験): 6-7
  - context / skill (調査知識): 5-7
  - preference / aversion: 4-6
- 出力は JSON 配列のみ (説明・前置き・コードフェンス禁止)
- 抽出対象が無ければ `[]`
- **撤回・変更の場合は `reason` フィールドを追加** (任意 / 該当なしなら省略):
  buffer 中に「過去には X と言っていた」 ような言質があり、 今回の発話がそれを
  撤回・上書きする内容なら、 撤回理由を 1 文で `reason` に書く.
  例: 旧「浅煎り派」 → 新「深煎り派」 (理由: 味の好みが変わったため).
    ```
    [{{"category": "preference", "key": "coffee_roast", "value": "深煎り",
       "importance": 5, "reason": "味の好みが変わったため"}}]
    ```
  単なる新規 fact (撤回伴わない) には reason を書かない.

## 議論ノード (オプショナル, 該当なしなら省略)
今回の発話が「論点 / 検討案 / 採用判断 / 撤回」 を含む場合は、 出力形式を
JSON object に切り替え、 `nodes` キーで議論ノードも一緒に出す:
- `kind`: "topic" (論点) / "option" (検討案) / "decision" (採用判断) /
  "retraction" (撤回) / "rationale" (理由)
- `title`: 短い見出し (15 字程度)
- `state`: "proposed" / "accepted" (採用済) / "rejected" / "observed"
- `content`: (任意) 詳細

例: 「Renju のカテゴリは自由作成で決定」 という発話:
```
{{
  "facts": [
    {{"category": "context", "key": "renju_category_decision",
      "value": "Renju カテゴリは自由作成で決定", "importance": 7}}
  ],
  "nodes": [
    {{"kind": "decision", "title": "Renju カテゴリ自由作成", "state": "accepted",
      "content": "テンプレ案を退け、 自由作成で確定"}}
  ]
}}
```

通常発話 (議論構造を含まない) では従来通り JSON 配列のみ出す:
`[{{"category": "...", ...}}]`

つまり nodes が無ければ array、 あれば object。 どちらでも parser は受け付ける.

## 入力

直近の会話:
{buffer}

今回の発話 ({role}):
{content}

## 出力
JSON 配列:"""


def build_prompt(role: str, content: str, buffer: list[dict]) -> str:
    if buffer:
        buf_lines = [f"[{m.get('role', '?')}] {m.get('content', '')}" for m in buffer]
        buf_text = "\n".join(buf_lines)
    else:
        buf_text = "(なし)"
    return _PROMPT_TEMPLATE.format(buffer=buf_text, role=role, content=content)


def _strip_code_fence(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)


def _extract_fact_items(text: str) -> list[dict]:
    """LLM 出力から fact 要素の list[dict] を取り出す.

    0.6.17 で出力形式が「JSON 配列 or `{"facts": [...], "nodes": [...]}` object」
    の 2 系統になったため、 どちらでも fact 配列を返す.
    """
    if not text:
        return []
    cleaned = _strip_code_fence(text)
    # まず object 形式を試す
    m_obj = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m_obj:
        try:
            data = json.loads(m_obj.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            facts = data.get("facts")
            if isinstance(facts, list):
                return [x for x in facts if isinstance(x, dict)]
    # 配列形式 (従来)
    m_arr = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if m_arr:
        try:
            data = json.loads(m_arr.group(0))
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    return []


def parse_response(text: str) -> list[FactCandidate]:
    """LLM 出力を FactCandidate のリストに変換 (object / array 両対応)."""
    out: list[FactCandidate] = []
    for item in _extract_fact_items(text):
        try:
            raw_reason = item.get("reason")
            reason: str | None = None
            if isinstance(raw_reason, str):
                s = raw_reason.strip()
                if s and s.lower() != "null":
                    reason = s
            fc = FactCandidate(
                category=str(item.get("category", "")).strip().lower(),
                key=str(item.get("key", "")).strip(),
                value=str(item.get("value", "")).strip(),
                importance=int(item.get("importance", 5)),
                reason=reason,
            )
        except (TypeError, ValueError):
            continue
        if fc.is_valid():
            out.append(fc)
    return out


def parse_nodes(text: str) -> list[NodeCandidate]:
    """LLM 出力から議論ノード候補を取り出す (0.6.17 案 1 Phase A2).

    出力が object 形式で `nodes` キーを持つ場合のみ返す. array 形式や
    nodes キー欠落の場合は空リスト.
    """
    if not text:
        return []
    cleaned = _strip_code_fence(text)
    m_obj = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m_obj:
        return []
    try:
        data = json.loads(m_obj.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list):
        return []
    out: list[NodeCandidate] = []
    for item in raw_nodes:
        if not isinstance(item, dict):
            continue
        try:
            nc = NodeCandidate(
                kind=str(item.get("kind", "")).strip().lower(),
                title=str(item.get("title", "")).strip(),
                state=str(item.get("state", "proposed")).strip().lower(),
                content=(
                    str(item["content"]).strip()
                    if isinstance(item.get("content"), str) and item["content"].strip()
                    else None
                ),
            )
        except (TypeError, ValueError):
            continue
        if nc.is_valid():
            out.append(nc)
    return out


def _extract_via_claude_backend(prompt: str) -> str:
    """`claude -p` を子プロセスとして呼び出し、出力テキストを返す.

    PERSONA_WRITE_BACKEND=claude の時に使う. ollama backend と同じ JSON 配列
    形式の出力を期待する (parse_response が共通で受け止める).
    """
    # 循環 import 回避のため関数内 import
    from scripts.escalate.claude_p import invoke_claude
    r = invoke_claude(prompt)
    return r.text if r.success else ""


def _generate_extract_raw(
    role: str, content: str, buffer: list[dict], client: LLMClient,
    model: str, backend: str,
) -> str:
    """write LLM を 1 回呼んで raw text を返す内部 helper.

    extract_facts と extract_facts_and_nodes が同じ呼び出し結果を共有するため
    切り出した. 失敗時は空文字.
    """
    prompt = build_prompt(role, content, buffer)
    try:
        if backend == "claude":
            return _extract_via_claude_backend(prompt)
        return client.generate(model, prompt, num_ctx=WRITE_NUM_CTX)
    except Exception:
        return ""


def extract_facts(
    role: str,
    content: str,
    buffer: list[dict],
    client: LLMClient,
    model: str = WRITE_MODEL,
    backend: str = WRITE_BACKEND,
) -> list[FactCandidate]:
    """write LLM で fact 候補を抽出.

    backend:
      'ollama' (default) — client.generate(model, prompt) で Ollama に投げる
      'claude'           — `claude -p prompt` 子プロセスに丸投げ (診断用)
    """
    raw = _generate_extract_raw(role, content, buffer, client, model, backend)
    return parse_response(raw)


def extract_facts_and_nodes(
    role: str,
    content: str,
    buffer: list[dict],
    client: LLMClient,
    model: str = WRITE_MODEL,
    backend: str = WRITE_BACKEND,
) -> tuple[list[FactCandidate], list[NodeCandidate]]:
    """fact + 議論ノードを同じ LLM 呼び出しから抽出 (0.6.17 案 1 Phase A2).

    LLM コール数は extract_facts と同じ (= 1). parse 側で fact / node を別解釈.
    """
    raw = _generate_extract_raw(role, content, buffer, client, model, backend)
    return parse_response(raw), parse_nodes(raw)
