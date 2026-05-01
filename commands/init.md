---
description: Set up a new persona (interactive — asks 9 questions via AskUserQuestion tool, name suggestions come from Ollama)
allowed-tools: Bash, Read, Write, AskUserQuestion
---

新しいペルソナをセットアップします。9 項目を **必ず AskUserQuestion ツールで** 1 つずつ聞いてください。

## 重要な操作ルール (絶対遵守)

- **各質問は AskUserQuestion で出す。プレーンテキストで「以下から選んでください」と列挙してはいけない。**
  理由: markdown bullet が `a. b. c.` に勝手にレンダリングされ、ユーザーが文字で答えても番号で答えても整合がずれる。クリック式 UI ならその問題が無い。
- 選択肢には自由入力を許す形にする (AskUserQuestion の自由入力フィールド or "その他" 選択肢) — 表に無いモデル名を指定したいケースに対応。
- 名前 (Q9) は AskUserQuestion 前に Ollama で 3 案生成してから、その 3 案を選択肢にして AskUserQuestion で聞く。

## 質問順序

名前は最後。役割・性格などを集めてから Ollama に渡してペルソナの雰囲気に合った名前候補を生成させ、最後にユーザーがその中から選ぶ。

---

### Q1. 軽量モデル (light)

AskUserQuestion で問う:

- **質問**: "書き込み時 (Stop / PreCompact / SessionEnd) で高頻度発火する軽量モデル。応答遅延を抑える役。"
- **選択肢 (header / description)**:
  - `gemma3:4b` / "推奨・~3.3GB / 高速"
  - `gemma3:1b` / "超軽量・~815MB / 最速だが粗い"
  - `qwen2.5:3b-instruct` / "~1.9GB / 日本語強"
  - `llama3.2:3b-instruct` / "~2.0GB"
  - `gemma3:12b` / "重量と同じ・品質優先派"
  - `その他` / "自由入力で別モデル名を指定"
- multiSelect: false

ユーザーが "その他" を選んだら、続けて自由入力 (AskUserQuestion) で実モデル名を聞く。

### Q2. 重量モデル (heavy)

- **質問**: "読み出し時 (proxy_recall 圧縮) で発火する重量モデル。出力がメインエージェントの context に直接注入されるため品質重視。"
- **選択肢**:
  - `gemma3:12b` / "推奨・~7GB / 品質高"
  - `gemma3:27b` / "大型・~16GB / 最高品質・要メモリ"
  - `qwen2.5:14b-instruct` / "~8.5GB / 日本語強"
  - `llama3.1:8b-instruct` / "~4.7GB"
  - `gemma3:4b` / "light と同じ / 軽量機派"
  - `その他` / "自由入力"

### Q3. 役割・立場

- **質問**: "このペルソナが何をする存在か"
- **選択肢**:
  - `バックエンドエンジニアの相棒`
  - `辛口コードレビュアー`
  - `仕様書・ドキュメントライター`
  - `リサーチャー (調査・要約担当)`
  - `メンター (教育・解説特化)`
  - `議論パートナー・壁打ち相手`
  - `プロダクトマネージャー視点の同僚`
  - `その他` / "自由入力"

### Q4. 性別

- **質問**: "性別 (口調の選択にも影響)"
- **選択肢**: `男性` / `女性` / `中性 / ノンバイナリー` / `指定なし`

### Q5. 性格

- **質問**: "性格の中心軸"
- **選択肢**:
  - `冷静沈着・論理的`
  - `明るく前向き`
  - `辛口・率直`
  - `慎重で丁寧`
  - `探究心旺盛`
  - `クール・寡黙`
  - `包容力ある聞き役`
  - `その他` / "自由入力"

### Q6. 一人称

- **質問**: "一人称"
- **選択肢**: `僕` / `俺` / `私` / `わたくし` / `我輩` / `拙者` / `うち` / `あたし` / `その他`

### Q7. 口調・話し方

- **質問**: "口調 (Q4 で選んだ性別との整合を意識すると自然)"
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

### Q8. ユーザーの呼び方

- **質問**: "ユーザー (あなた) をペルソナはどう呼ぶか"
- **選択肢**: `あなた` / `君` / `お前` / `〜さん (敬称)` / `〜様` / `マスター` / `ご主人` / `その他`

### Q9. 名前 (動的生成)

ここまでの 8 つが揃ったら、**Ollama でペルソナ名を 3 つ生成** してから AskUserQuestion で問う:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
PERSONA_JUDGE_MODEL="<Q1 で選んだ light モデル>" \
  python3 "$PLUGIN_ROOT/scripts/suggest_names.py" \
    --role "<Q3>" --gender "<Q4>" --personality "<Q5>" \
    --first-person "<Q6>" --speech-style "<Q7>" 2>/dev/null
```

3 行 (3 案) 出力されるので、それらを選択肢にして AskUserQuestion:

- **質問**: "ペルソナの名前 (生成案または自由入力)"
- **選択肢**: `<生成案 1>` / `<生成案 2>` / `<生成案 3>` / `<basename(pwd)> (ディレクトリ名そのまま)` / `その他 (自由入力)`

Ollama が失敗 (空出力) した場合は素直に AskUserQuestion を自由入力モードで出して名前を聞く。

---

## セットアップ実行

9 問すべて揃ったら、以下を順に Bash で実行:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
DATA_DIR="${CLAUDE_PLUGIN_DATA:-$PLUGIN_ROOT/data}"

NAME="<Q9 で決まった名前>"
LIGHT="<Q1>"
HEAVY="<Q2>"

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
PERSONA_EMBED_MODEL="nomic-embed-text"
OLLAMA_HOST="http://localhost:11434"
EOF

# 3. このペルソナをアクティブに
echo "$NAME" > "$DATA_DIR/active-persona"

# 4. persona facts を seed
PERSONA_MEMORY_DB="$DATA_DIR/$NAME.db" \
  "$PLUGIN_ROOT/.venv/bin/python" "$PLUGIN_ROOT/scripts/seed_persona.py" \
  --name "$NAME" \
  --role "<Q3>" --gender "<Q4>" --personality "<Q5>" \
  --first-person "<Q6>" --speech-style "<Q7>" \
  --address-user "<Q8>"
```

完了後ユーザーへ:
- 完了サマリー (名前 / light / heavy / DB パス / config.env)
- **Claude Code を完全終了 → 再起動**で MCP server と hook がロードされる
- 次回起動時から SessionStart で persona facts が自動注入される
