---
description: Set up a new persona (asks 7 persona questions interactively; gender-aware options; uses recommended Ollama models by default)
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
PERSONA_MEMORY_DB="$PERSONA_DIR/$NAME.db" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" "$PLUGIN_ROOT/scripts/seed_persona.py" \
  --db "$PERSONA_DIR/$NAME.db" \
  --name "$NAME" \
  --role "<Q1>" --gender "<Q2>" --personality "<Q3>" \
  --first-person "<Q5>" --speech-style "<Q6>" \
  --address-user "<Q4>"

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

- 完了サマリー (名前 / 役割 / 性格 / 一人称 / 口調 / DB パス)
- 使ったモデル: `gemma3:4b` (light = write LLM) + `gemma3:12b` (heavy = recall LLM) + `nomic-embed-text` (embed)
- 変更したい場合は `/persona-memory:configure-models`
- **Claude Code を完全終了 → 再起動** で hook がロードされる
- 次回起動時から SessionStart で persona facts が自動注入される
