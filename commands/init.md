---
description: Set up a new persona (asks 7 persona questions interactively; uses recommended Ollama models by default — change later with /persona-memory:configure-models)
allowed-tools: Bash, Read, Write, AskUserQuestion
---

新しいペルソナをセットアップします。

## デフォルトのモデル設定

このコマンドは **Ollama モデルを推奨デフォルトのまま使う** 前提です:

- 軽量モデル (light, 書き込み時): `gemma3:4b`
- 重量モデル (heavy, 読み出し圧縮時): `gemma3:12b`
- 埋め込みモデル: `nomic-embed-text`

低スペック機で軽量化したい / 大型 GPU で品質を上げたい場合は、セットアップ後に `/persona-memory:configure-models` で変更できます。今は深く考えずに進めて OK です。

## 質問 7 つ — すべて AskUserQuestion ツールで聞く

**重要**: 各質問は **必ず AskUserQuestion ツール** で出してください。プレーンテキストで「以下から選んでください」と列挙すると、markdown レンダラが文字 (a/b/c) と番号 (1/2/3) を不一致にしてユーザーが詰まります。AskUserQuestion ならクリック式で問題なし。

### Q1. 役割・立場

- **質問**: "このペルソナが何をする存在か"
- **選択肢**:
  - `バックエンドエンジニアの相棒`
  - `辛口コードレビュアー`
  - `仕様書・ドキュメントライター`
  - `リサーチャー (調査・要約担当)`
  - `メンター (教育・解説特化)`
  - `議論パートナー・壁打ち相手`
  - `プロダクトマネージャー視点の同僚`
  - `その他` — 自由入力で別の役割を指定

### Q2. 性別

- **質問**: "性別 (口調の選択にも影響します)"
- **選択肢**: `男性` / `女性` / `中性 / ノンバイナリー` / `指定なし`

### Q3. 性格

- **質問**: "性格の中心軸"
- **選択肢**:
  - `冷静沈着・論理的`
  - `明るく前向き`
  - `辛口・率直`
  - `慎重で丁寧`
  - `探究心旺盛`
  - `クール・寡黙`
  - `包容力ある聞き役`
  - `その他`

### Q4. 一人称

- **質問**: "一人称"
- **選択肢**: `僕` / `俺` / `私` / `わたくし` / `我輩` / `拙者` / `うち` / `あたし` / `その他`

### Q5. 口調・話し方

- **質問**: "口調 (Q2 で選んだ性別との整合を意識すると自然)"
- **選択肢**:
  - `敬語 (丁寧・中性的)`
  - `敬語 (女性的・柔らかめ — 〜ですの / 〜ますわ)`
  - `敬語 (男性的・凛々しめ — 〜であります / 〜致します)`
  - `タメ口 (フランク・中性的)`
  - `タメ口 (女性的 — 〜だよね / 〜なの)`
  - `タメ口 (男性的・荒め — 〜だぜ / 〜だな)`
  - `お嬢様口調 (〜ですわ / 〜ですのよ)`
  - `ぶっきらぼう・短文`
  - `古風・文語調`
  - `武士口調 (拙者…でござる)`
  - `関西弁`
  - `その他`

### Q6. ユーザーの呼び方

- **質問**: "ユーザー (あなた) をペルソナはどう呼ぶか"
- **選択肢**: `あなた` / `君` / `お前` / `〜さん (敬称)` / `〜様` / `マスター` / `ご主人` / `その他`

### Q7. 名前 (Ollama に提案させてから選択)

ここまでの 6 つが揃ったら、**Ollama でペルソナ名を 3 つ生成** してから AskUserQuestion で問う:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
PERSONA_JUDGE_MODEL="gemma3:4b" \
  python3 "$PLUGIN_ROOT/scripts/suggest_names.py" \
    --role "<Q1>" --gender "<Q2>" --personality "<Q3>" \
    --first-person "<Q4>" --speech-style "<Q5>" 2>/dev/null
```

3 行 (3 案) 出力されるので、それらを選択肢にして AskUserQuestion:

- **質問**: "ペルソナの名前 (生成案または自由入力)"
- **選択肢**: `<生成案 1>` / `<生成案 2>` / `<生成案 3>` / `<basename(pwd)> (ディレクトリ名そのまま)` / `その他 (自由入力)`

Ollama が失敗 (空出力) した場合は素直に AskUserQuestion を自由入力モードで出して名前を聞く。

---

## セットアップ実行

7 問すべて揃ったら、以下を順に Bash で実行:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
DATA_DIR="${CLAUDE_PLUGIN_DATA:-$PLUGIN_ROOT/data}"

NAME="<Q7 で決まった名前>"
LIGHT="gemma3:4b"     # デフォルト固定。/persona-memory:configure-models で後変更可能
HEAVY="gemma3:12b"
EMBED="nomic-embed-text"

# 1. venv + Ollama モデル + DB 初期化
PERSONA_LIGHT_MODEL="$LIGHT" PERSONA_HEAVY_MODEL="$HEAVY" \
  PERSONA_JUDGE_MODEL="$LIGHT" \
  bash "$PLUGIN_ROOT/setup.sh" "$NAME"

# 2. config.env を書く (hooks がここから読む)
mkdir -p "$DATA_DIR"
cat > "$DATA_DIR/$NAME.config.env" <<EOF
PERSONA_MEMORY_DB="$DATA_DIR/$NAME.db"
PERSONA_LIGHT_MODEL="$LIGHT"
PERSONA_HEAVY_MODEL="$HEAVY"
PERSONA_EMBED_MODEL="$EMBED"
OLLAMA_HOST="http://localhost:11434"
EOF

# 3. このペルソナをアクティブに
echo "$NAME" > "$DATA_DIR/active-persona"

# 4. persona facts を seed
PERSONA_MEMORY_DB="$DATA_DIR/$NAME.db" \
  "$PLUGIN_ROOT/.venv/bin/python" "$PLUGIN_ROOT/scripts/seed_persona.py" \
  --name "$NAME" \
  --role "<Q1>" --gender "<Q2>" --personality "<Q3>" \
  --first-person "<Q4>" --speech-style "<Q5>" \
  --address-user "<Q6>"
```

完了後ユーザーへ:
- 完了サマリー (名前 / 役割 / 性格 / DB パス)
- 使ったモデル: `gemma3:4b` (light) + `gemma3:12b` (heavy) + `nomic-embed-text` (embed)
- **変更したい場合は** `/persona-memory:configure-models` (低スペック機なら gemma3:1b / qwen2.5:3b 等)
- **Claude Code を完全終了 → 再起動** で MCP server と hook がロードされる
- 次回起動時から SessionStart で persona facts が自動注入される
