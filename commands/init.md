---
description: Set up a new persona (7 persona questions + 5 stance axes interactively; gender-aware options; uses recommended Ollama models by default)
allowed-tools: Bash, Read, Write, AskUserQuestion
---

新しいペルソナをセットアップします。

## Step 0: 既存ペルソナの検出 (再実行時の挙動制御)

最初に、このプロジェクトに既にペルソナが存在しないかチェックする。

```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
DATA_DIR="$PROJECT_DIR/.persona-memory"
ACTIVE=""
[ -r "$DATA_DIR/active-persona" ] && ACTIVE="$(cat "$DATA_DIR/active-persona" 2>/dev/null)"

if [ -n "$ACTIVE" ] && [ -f "$DATA_DIR/$ACTIVE.db" ]; then
  facts=$(sqlite3 "$DATA_DIR/$ACTIVE.db" \
    "SELECT COUNT(*) FROM facts WHERE status='active' AND category='persona'" 2>/dev/null)
  total=$(sqlite3 "$DATA_DIR/$ACTIVE.db" \
    "SELECT COUNT(*) FROM facts WHERE status='active'" 2>/dev/null)
  eps=$(sqlite3 "$DATA_DIR/$ACTIVE.db" "SELECT COUNT(*) FROM episodes" 2>/dev/null)
  echo "===== 既存ペルソナ検出 ====="
  echo "active:    $ACTIVE"
  echo "persona facts: $facts"
  echo "active facts (total): $total"
  echo "episodes:  $eps"
fi
```

**`ACTIVE` が空 (ペルソナ未登録) の場合**: そのまま下の「Batch 1」 に進む。

**`ACTIVE` がある場合**: AskUserQuestion で実行モードを選ばせる:

- **header**: `init モード`
- **question**: "現在『$ACTIVE』 がアクティブです。どうしますか?"
- **multiSelect**: false
- **options**:
  1. label: `同名で persona facts のみ更新` / description: "DB と episodes は維持。persona category の 9 facts を新しい質問回答で上書きする"
  2. label: `同名で完全リセット` / description: "DB を削除して新規作成。episodes / 全 facts が消える (復元不能)"
  3. label: `別名で新規追加` / description: "既存ペルソナは残し、別 DB を作って active を切り替える"
  4. label: `キャンセル` / description: "何もしない"

選択結果に応じた前処理:

- **「同名で persona facts のみ更新」**:
  - **既存の persona / rule category の active 行を一括で superseded に降格** (boot 層 = Batch 1〜7 の seed が UNIQUE(category, key) WHERE active 制約を踏まないようにするため & 過去のキャンセル init で write LLM が混入させた重複 boot facts を一掃するため)
  - episodes / 他 category の facts は残す
  - Q7 の名前は ACTIVE で固定 (再質問しない)
  ```bash
  sqlite3 "$DATA_DIR/$ACTIVE.db" \
    "UPDATE facts SET status='superseded', \
                      updated_at=datetime('now', '+9 hours') \
     WHERE category IN ('persona', 'rule') AND status='active'"
  echo "[update] 既存 boot 層 (persona / rule) を superseded に降格、新 seed に進みます"
  ```
- **「同名で完全リセット」**: 以下を実行してから Batch 1 へ。
  ```bash
  rm -f "$DATA_DIR/$ACTIVE.db" "$DATA_DIR/$ACTIVE.config.env"
  rm -f "$DATA_DIR/active-persona" "$DATA_DIR/debug-recall.log"
  echo "[reset] $ACTIVE を削除しました。新規セットアップに進みます"
  ```
- **「別名で新規追加」**: 既存はそのまま、Batch 1 〜 Q7 で新名を取得して新規セットアップ。最後の active-persona 切替で新名が active になる
- **「キャンセル」**: ここで終了。"キャンセルしました。" と返して何もしない

## デフォルトのモデル設定

このコマンドは Ollama モデルを推奨デフォルト (`gemma3:4b` / `gemma3:12b` / `nomic-embed-text`) で使う前提です。後で変更したい場合は `/persona-memory:configure-models` で変更できます。

## AskUserQuestion の制約 (絶対遵守)

- **1 質問あたり最大 4 オプション**。それ以上は切られる。
- **各オプションに `label` と `description` を必ず明示する**。description を空 / 省略すると agent が補完してハルシネーションを起こす。**以下の文言を逐語的に使い、勝手に書き換えない。**
- **「その他」「Other」「自由入力」を選択肢に含めない**。tool が自動で追加してくれる。
- **複数質問を 1 回の AskUserQuestion 呼び出しでバッチ可能 (最大 4 questions)**。関連する質問はまとめると UX が良い。
- **`header` は 12 文字以内** の超短いタグ。

## 性別ベースの選択肢フィルタ (重要)

選択肢が 4 個しかないので、Q2 (性別) の答えに応じて Q5 (一人称) と Q6 (口調) のオプションを **動的に変える**。これで「男性なのに『あたし』が候補に出る」「女性なのに『〜であります』が出る」といった不整合を回避する。下に各ケースの選択肢を表として書いてあるので、その通りに渡す。

---

## Batch 1: 役割 + 性別 + 性格 + 呼称 を 1 回の AskUserQuestion で

`questions` 配列に以下 4 個を入れて呼び出す:

### Q1. 役割

- **header**: `役割`
- **question**: "このペルソナがあなたにとって何をする存在か？"
- **multiSelect**: false
- **options**:
  1. label: `バックエンド相棒` / description: "API・DB・パフォーマンス改善で並走する開発パートナー"
  2. label: `辛口コードレビュアー` / description: "PR を率直に審査して設計の弱点を指摘する役"
  3. label: `壁打ちパートナー` / description: "アイデアを投げて反応や反論を返してもらう議論役"
  4. label: `メンター` / description: "知識を体系的に解説する先生役"

### Q2. 性別

- **header**: `性別`
- **question**: "ペルソナの性別 (一人称や口調の選択肢にも影響します)"
- **multiSelect**: false
- **options**:
  1. label: `男性` / description: "代名詞や口調が男性寄りになる"
  2. label: `女性` / description: "代名詞や口調が女性寄りになる"
  3. label: `中性` / description: "性別を強く出さないノンバイナリー寄り"
  4. label: `指定なし` / description: "性別を意識せずペルソナに任せる"

### Q3. 性格

- **header**: `性格`
- **question**: "性格の中心軸"
- **multiSelect**: false
- **options**:
  1. label: `冷静沈着・論理的` / description: "感情を抑え、根拠ベースで議論する"
  2. label: `探究心旺盛` / description: "技術や仕組みを深掘りしたがる"
  3. label: `辛口・率直` / description: "オブラートに包まず、問題点を直接指摘する"
  4. label: `慎重で丁寧` / description: "確認を重ねて手堅く進める"

### Q4. 呼称

- **header**: `呼称`
- **question**: "ペルソナはあなたをどう呼ぶか"
- **multiSelect**: false
- **options**:
  1. label: `〜さん (敬称)` / description: "礼儀正しい汎用的な呼び方"
  2. label: `あなた` / description: "中性、フォーマル寄り"
  3. label: `君` / description: "親しみと、やや上からの距離感"
  4. label: `マスター` / description: "主従関係を演出する特殊な呼び方"

---

## Batch 1B: 立ち位置 5 軸 (思考傾向スライダー)

性格 (Q3) の直後の文脈で **立ち位置** を 5 軸聞く. 各軸 4 オプション
(`強く A` / `やや A` / `やや B` / `強く B`) で、 「バランス」 を選びたい
ユーザーは tool が自動で追加する **「Other」 で 0 と入力** する.

複数ペルソナを使う時に思考傾向を意図的に散らして視野狭窄を防ぐための設定.
各軸は応答スタイルに反映される (boot 層 `persona/stance` に注入).

**実装メモ**: 5 軸を 2 batch (3 軸 + 2 軸) に分けて AskUserQuestion を 2 回呼ぶ
(1 batch あたり最大 4 questions 制約のため). 各軸の値は -2 / -1 / 0 / +1 / +2
の整数で内部保持する.

### Batch 1B-1: 3 軸 を 1 回の AskUserQuestion で

#### S1. 保守 ↔ 革新

- **header**: `軸 1/5`
- **question**: "提案の傾向 (新規性をどれだけ強調するか)"
- **multiSelect**: false
- **options**:
  1. label: `強く保守 (-2)` / description: "実績ある手段や既存パターンを強く優先、不確実な提案は避ける"
  2. label: `やや保守 (-1)` / description: "原則として既存パターン優先、たまに別案も触れる"
  3. label: `やや革新 (+1)` / description: "標準解も示しつつ別解・新案を積極的に提案する"
  4. label: `強く革新 (+2)` / description: "既存パターンより新しい解決経路を優先して提示する"

#### S2. 楽観 ↔ 悲観

- **header**: `軸 2/5`
- **question**: "提案時のトーン (リスクと可能性のどちらを先に示すか)"
- **multiSelect**: false
- **options**:
  1. label: `強く楽観 (-2)` / description: "うまくいく前提で前向きに、可能性を強調する"
  2. label: `やや楽観 (-1)` / description: "原則前向き、リスクは聞かれたら答える"
  3. label: `やや悲観 (+1)` / description: "リスクや問題点を先に挙げてから案を出す"
  4. label: `強く悲観 (+2)` / description: "失敗パターン・落とし穴を最優先で警告する"

#### S3. 直感 ↔ 分析

- **header**: `軸 3/5`
- **question**: "結論の出し方 (経験則で素早く vs 根拠を積んでから)"
- **multiSelect**: false
- **options**:
  1. label: `強く直感 (-2)` / description: "経験則や勘で素早く方向を示す、詳細検証は後回し"
  2. label: `やや直感 (-1)` / description: "原則経験則ベース、必要時のみ検証を加える"
  3. label: `やや分析 (+1)` / description: "簡単な根拠を示してから結論を述べる"
  4. label: `強く分析 (+2)` / description: "データ・仕様・原理を先に示してから論理的に結論を導く"

### Batch 1B-2: 2 軸 を 1 回の AskUserQuestion で

#### S4. 慎重 ↔ 大胆

- **header**: `軸 4/5`
- **question**: "進め方 (確認重視 vs 踏み込み重視)"
- **multiSelect**: false
- **options**:
  1. label: `強く慎重 (-2)` / description: "確認・段階分割・小実験を必ず重ねて手堅く進める"
  2. label: `やや慎重 (-1)` / description: "原則手堅く、明らかに安全な時のみ踏み込む"
  3. label: `やや大胆 (+1)` / description: "リスクは挙げるが踏み込んだ提案を先に出す"
  4. label: `強く大胆 (+2)` / description: "まず踏み込んだ提案、リスクは事後に補足"

#### S5. 共感 ↔ 論理

- **header**: `軸 5/5`
- **question**: "言い回しの軸 (ユーザー状況への配慮 vs 論理一貫性)"
- **multiSelect**: false
- **options**:
  1. label: `強く共感 (-2)` / description: "ユーザー状況・感情・意図を最優先で配慮"
  2. label: `やや共感 (-1)` / description: "原則配慮しつつ論理も示す"
  3. label: `やや論理 (+1)` / description: "論理を主軸に、配慮は補足程度"
  4. label: `強く論理 (+2)` / description: "感情に流されず論理・整合性・原理原則を貫く"

### Stance の数値変換 (内部処理)

ユーザーの選択をパースして 5 要素 int list `[v1, v2, v3, v4, v5]` を作る:

- ラベル末尾の `(-2)` / `(-1)` / `(+1)` / `(+2)` を抽出して int 化
- 「Other」 で自由入力した場合は **数値文字列をそのまま** 受け取り int 化 (= 0 等)
- 範囲外 (-2..+2 外) は警告してデフォルト 0 にフォールバック

後段の seed_persona.py 呼び出しで `--stance "v1,v2,v3,v4,v5"` を渡す.

---

## Batch 2: 一人称 + 口調 を 1 回の AskUserQuestion で (性別フィルタ適用)

Q2 で得た **性別の値に応じて以下の表から選択肢を選んで** AskUserQuestion を呼ぶ。description は逐語的に使うこと。

### Q5. 一人称 (性別別)

#### 性別 = 男性
- **options**:
  1. label: `僕` / description: "やや柔らかい、男性的"
  2. label: `俺` / description: "フランク、男性的"
  3. label: `私` / description: "敬語と相性が良い、中性的"
  4. label: `我輩` / description: "古風・大物感"

#### 性別 = 女性
- **options**:
  1. label: `私` / description: "敬語と相性が良い、中性的"
  2. label: `わたくし` / description: "格式高い、特別丁寧"
  3. label: `あたし` / description: "親しみのある女性的な一人称"
  4. label: `うち` / description: "関西寄り・カジュアルな女性"

#### 性別 = 中性 / 指定なし
- **options**:
  1. label: `私` / description: "敬語と相性が良い、中性的"
  2. label: `僕` / description: "やや柔らかい、若干男性寄り"
  3. label: `わたくし` / description: "格式高い、特別丁寧"
  4. label: `俺` / description: "フランク、男性寄り"

共通の **header**: `一人称`
共通の **question**: "ペルソナが自分を指す一人称"
共通の **multiSelect**: false

### Q6. 口調 (性別別)

#### 性別 = 男性
- **options**:
  1. label: `敬語 (中性)` / description: "標準的な〜です／〜ます調"
  2. label: `敬語 (男性的)` / description: "〜であります／〜致します で凛々しい"
  3. label: `タメ口 (中性)` / description: "口語体、親しみやすい"
  4. label: `タメ口 (男性的)` / description: "〜だぜ／〜だな で荒め"

#### 性別 = 女性
- **options**:
  1. label: `敬語 (中性)` / description: "標準的な〜です／〜ます調"
  2. label: `敬語 (女性的)` / description: "〜ですの／〜ますわ で柔らかい"
  3. label: `タメ口 (女性的)` / description: "〜だよね／〜なの で親しみやすい"
  4. label: `お嬢様口調` / description: "〜ですわ／〜ですのよ で上品"

#### 性別 = 中性 / 指定なし
- **options**:
  1. label: `敬語 (中性)` / description: "標準的な〜です／〜ます調"
  2. label: `タメ口 (中性)` / description: "口語体、親しみやすい"
  3. label: `敬語 (女性的)` / description: "〜ですの／〜ますわ で柔らかい"
  4. label: `敬語 (男性的)` / description: "〜であります／〜致します で凛々しい"

共通の **header**: `口調`
共通の **question**: "話し方のスタイル"
共通の **multiSelect**: false

> 上記 4 オプションに無い口調 (古風・武士口調・関西弁等) を使いたいユーザーは tool が自動追加する **「Other」を選んで自由入力** できる。

---

## ペルソナ名候補を Ollama で生成

ここまでの 6 つが揃ったら、以下を Bash で実行して名前候補を取得:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
PERSONA_JUDGE_MODEL="gemma3:4b" \
  python3 "$PLUGIN_ROOT/scripts/suggest_names.py" \
    --role "<Q1 のラベル>" \
    --gender "<Q2 のラベル>" \
    --personality "<Q3 のラベル>" \
    --first-person "<Q5 のラベル>" \
    --speech-style "<Q6 のラベル>" 2>/dev/null
```

3 行 (3 案) が出力される。空ならフォールバックで自由入力で名前を聞く (AskUserQuestion を使わず、テキストプロンプトで直接質問)。

## Batch 3: 名前 (最後の AskUserQuestion)

### Q7. 名前

- **header**: `ペルソナ名`
- **question**: "ペルソナの名前 (Ollama 提案 3 案 + ディレクトリ名)"
- **multiSelect**: false
- **options** (3 案 + ディレクトリ名 = 4 個):
  1. label: `<Ollama 案 1>` / description: "Ollama がペルソナ設定から提案"
  2. label: `<Ollama 案 2>` / description: "Ollama がペルソナ設定から提案"
  3. label: `<Ollama 案 3>` / description: "Ollama がペルソナ設定から提案"
  4. label: `<basename(pwd)>` / description: "ディレクトリ名そのまま"

> 自分で考えた名前にしたいユーザーは「Other」で自由入力。

---

## auto-memory 削除 + recap 無効化の案内 (Claude Code 標準機能との衝突回避)

セットアップ実行 (下記) 前後に、ユーザーに以下を **明示的に確認・案内** すること:

### auto-memory が検出された場合 (Step 5 の出力で `Claude Code 標準 auto-memory を検出` が出た時)

AskUserQuestion で:

- **header**: `auto-memory`
- **question**: "Claude Code 標準 auto-memory ディレクトリ (`<検出パス>`) を削除しますか? 並存すると記憶が分散します"
- **multiSelect**: false
- **options**:
  1. label: `削除する` / description: "ファイル全削除。これ以降は persona-memory DB に集約される"
  2. label: `残す` / description: "今のまま並存させる (記憶分散リスクあり)"

「削除する」 を選んだら:

```bash
rm -rf "$CC_MEMORY_DIR"
echo "[ok] $CC_MEMORY_DIR を削除しました"
```

### recap (会話要約) 無効化の案内

Claude Code 標準の **recap 機能** も auto-memory と同じ理由で無効化推奨。
セットアップ完了報告の **末尾に必ず** 以下の案内を含めること:

> 📌 **追加の手動設定をお願いします**:
>
> Claude Code 標準の "recap" 機能 (会話末尾に直近トピックを表示する機能) も
> persona-memory と並存すると記憶経路が分散します。**`/config`** で
> "Show recaps" 等の項目を **無効化** してください。

## セットアップ実行

7 問すべて揃ったら、以下を順に Bash で実行:

```bash
# Plugin location: Claude Code's Bash tool does NOT expose CLAUDE_PLUGIN_ROOT
# (only hooks/MCP servers receive it). Auto-discover from the cache path
# instead — pick the latest version directory we can find.
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$PLUGIN_ROOT" ] || [ ! -d "$PLUGIN_ROOT" ]; then
  PLUGIN_ROOT=$(ls -d "$HOME"/.claude/plugins/cache/persona-memory/persona-memory/*/ 2>/dev/null \
                | sort -V | tail -1 | sed 's|/$||')
fi
if [ -z "$PLUGIN_ROOT" ] || [ ! -d "$PLUGIN_ROOT" ]; then
  echo "ERROR: persona-memory plugin cache not found. Run /plugin install first." >&2
  exit 1
fi

# Shared Python venv lives in $CLAUDE_PLUGIN_DATA. Derive from the cache
# convention — Bash tool doesn't get CLAUDE_PLUGIN_DATA directly.
case "$PLUGIN_ROOT" in
  */plugins/cache/*/*/*)
    _plugin_dir="$(dirname "$PLUGIN_ROOT")"
    _market_dir="$(dirname "$_plugin_dir")"
    _plugins_root="$(dirname "$(dirname "$_market_dir")")"
    VENV_HOME="$_plugins_root/data/$(basename "$_market_dir")-$(basename "$_plugin_dir")"
    ;;
  *) VENV_HOME="$PLUGIN_ROOT" ;;
esac
mkdir -p "$VENV_HOME"

# Project-local persona memory dir. Each project has its own persona.
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
PERSONA_DIR="$PROJECT_DIR/.persona-memory"
mkdir -p "$PERSONA_DIR"

NAME="<Q7 で決まった名前>"
LIGHT="gemma3:4b"     # デフォルト固定。/persona-memory:configure-models で後変更可能
HEAVY="gemma3:12b"
EMBED="nomic-embed-text"

# 1. shared venv + Ollama モデル + project-local DB 初期化
CLAUDE_PLUGIN_DATA="$VENV_HOME" CLAUDE_PROJECT_DIR="$PROJECT_DIR" \
  PERSONA_LIGHT_MODEL="$LIGHT" PERSONA_HEAVY_MODEL="$HEAVY" \
  PERSONA_JUDGE_MODEL="$LIGHT" \
  bash "$PLUGIN_ROOT/setup.sh" "$NAME"

# 2. config.env を書く (hooks がここから読む)
cat > "$PERSONA_DIR/$NAME.config.env" <<EOF
PERSONA_MEMORY_DB="$PERSONA_DIR/$NAME.db"
PERSONA_LIGHT_MODEL="$LIGHT"
PERSONA_HEAVY_MODEL="$HEAVY"
PERSONA_EMBED_MODEL="$EMBED"
OLLAMA_HOST="http://localhost:11434"
EOF

# 3. このペルソナをアクティブに
echo "$NAME" > "$PERSONA_DIR/active-persona"

# 4. persona facts を seed (新スキーマ — scripts.* を import するため
#    PYTHONPATH に PLUGIN_ROOT を渡す)
#    Batch 1B (立ち位置 5 軸) で得た値を `--stance "v1,v2,v3,v4,v5"` で渡す.
#    省略可 (= persona/stance fact が seed されず、 後方互換動作).
PERSONA_MEMORY_DB="$PERSONA_DIR/$NAME.db" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" "$PLUGIN_ROOT/scripts/seed_persona.py" \
  --db "$PERSONA_DIR/$NAME.db" \
  --name "$NAME" \
  --role "<Q1>" --gender "<Q2>" --personality "<Q3>" \
  --first-person "<Q5>" --speech-style "<Q6>" \
  --address-user "<Q4>" \
  --stance "<S1>,<S2>,<S3>,<S4>,<S5>"

# 5. Claude Code 標準 auto-memory との衝突チェック
#    persona-memory プラグインは独自 DB に記憶を集約する方針なので、
#    並行して Claude Code 標準 auto-memory が動いていると記憶が分散する。
#    init 時に既存の auto-memory を削除する案内を出す (/upgrade では触らない)。
PROJECT_KEY=$(printf '%s' "$PROJECT_DIR" | sed 's|/|-|g')
CC_MEMORY_DIR="$HOME/.claude/projects/${PROJECT_KEY}/memory"
if [ -d "$CC_MEMORY_DIR" ] && [ -n "$(ls -A "$CC_MEMORY_DIR" 2>/dev/null)" ]; then
  echo
  echo "===== ⚠️ Claude Code 標準 auto-memory を検出 ====="
  echo "  パス: $CC_MEMORY_DIR"
  echo "  ファイル数: $(ls -A "$CC_MEMORY_DIR" 2>/dev/null | wc -l | tr -d ' ')"
  echo
  echo "  persona-memory プラグインは独自 DB に記憶を集約します。"
  echo "  auto-memory が並存していると記憶が分散して呼び出せなくなります。"
fi

# 6. .gitignore に .persona-memory/ を追加するか提案 (個人記憶を git に上げない)
if [ -d "$PROJECT_DIR/.git" ] && ! grep -q "^\.persona-memory/$" "$PROJECT_DIR/.gitignore" 2>/dev/null; then
  echo ".persona-memory/" >> "$PROJECT_DIR/.gitignore"
  echo "[ok] added .persona-memory/ to $PROJECT_DIR/.gitignore"
fi
```

完了後ユーザーへ:

- 完了サマリー (名前 / 役割 / 性格 / 一人称 / 口調 / 立ち位置 5 軸の値 / DB パス)
- 使ったモデル: `gemma3:4b` (light = write LLM) + `gemma3:12b` (heavy = recall LLM) + `nomic-embed-text` (embed)
- 変更したい場合は `/persona-memory:configure-models`
- **Claude Code を完全終了 → 再起動** で hook がロードされる
- 次回起動時から SessionStart で persona facts が自動注入される
