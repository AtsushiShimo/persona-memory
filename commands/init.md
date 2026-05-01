---
description: Set up a new persona (asks 7 persona questions interactively; gender-aware options; uses recommended Ollama models by default)
allowed-tools: Bash, Read, Write, AskUserQuestion
---

新しいペルソナをセットアップします。

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

## セットアップ実行

7 問すべて揃ったら、以下を順に Bash で実行:

```bash
# CLAUDE_PLUGIN_ROOT は Bash tool 環境にも plugin context で渡される。
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"

# CLAUDE_PLUGIN_DATA は Bash tool には渡らないので、convention から計算する:
#   ~/.claude/plugins/cache/<market>/<plugin>/<version>  ← CLAUDE_PLUGIN_ROOT
#   ~/.claude/plugins/data/<market>-<plugin>             ← 永続 data dir
case "$PLUGIN_ROOT" in
  */plugins/cache/*/*/*)
    _plugin_dir="$(dirname "$PLUGIN_ROOT")"
    _market_dir="$(dirname "$_plugin_dir")"
    _plugins_root="$(dirname "$(dirname "$_market_dir")")"
    DATA_DIR="$_plugins_root/data/$(basename "$_market_dir")-$(basename "$_plugin_dir")"
    ;;
  *)
    # standalone form fallback
    DATA_DIR="$PLUGIN_ROOT/data"
    ;;
esac
mkdir -p "$DATA_DIR"

NAME="<Q7 で決まった名前>"
LIGHT="gemma3:4b"     # デフォルト固定。/persona-memory:configure-models で後変更可能
HEAVY="gemma3:12b"
EMBED="nomic-embed-text"

# 1. venv + Ollama モデル + DB 初期化
#    setup.sh は CLAUDE_PLUGIN_DATA があれば $DATA_DIR/.venv に venv を作る
#    (plugin version bump で cache が消えても venv は残る設計)
CLAUDE_PLUGIN_DATA="$DATA_DIR" \
  PERSONA_LIGHT_MODEL="$LIGHT" PERSONA_HEAVY_MODEL="$HEAVY" \
  PERSONA_JUDGE_MODEL="$LIGHT" \
  bash "$PLUGIN_ROOT/setup.sh" "$NAME"

# 2. config.env を書く (hooks がここから読む)
cat > "$DATA_DIR/$NAME.config.env" <<EOF
PERSONA_MEMORY_DB="$DATA_DIR/$NAME.db"
PERSONA_LIGHT_MODEL="$LIGHT"
PERSONA_HEAVY_MODEL="$HEAVY"
PERSONA_EMBED_MODEL="$EMBED"
OLLAMA_HOST="http://localhost:11434"
EOF

# 3. このペルソナをアクティブに
echo "$NAME" > "$DATA_DIR/active-persona"

# 4. persona facts を seed (venv は永続データ側にある)
PERSONA_MEMORY_DB="$DATA_DIR/$NAME.db" \
  "$DATA_DIR/.venv/bin/python" "$PLUGIN_ROOT/scripts/seed_persona.py" \
  --name "$NAME" \
  --role "<Q1>" --gender "<Q2>" --personality "<Q3>" \
  --first-person "<Q5>" --speech-style "<Q6>" \
  --address-user "<Q4>"
```

完了後ユーザーへ:

- 完了サマリー (名前 / 役割 / 性格 / 一人称 / 口調 / DB パス)
- 使ったモデル: `gemma3:4b` (light) + `gemma3:12b` (heavy) + `nomic-embed-text` (embed)
- 変更したい場合は `/persona-memory:configure-models`
- **Claude Code を完全終了 → 再起動** で MCP server と hook がロードされる
- 次回起動時から SessionStart で persona facts が自動注入される
