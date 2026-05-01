---
description: Set up a new persona (interactive — asks 9 questions, name comes last and is suggested by Ollama based on earlier answers)
allowed-tools: Bash, Read, Write
---

新しいペルソナをセットアップします。

# 質問順序の方針

**名前は最後**に聞きます。先に役割・性格・口調などを集めてから、Ollama にそれらを渡してペルソナの雰囲気に合った名前候補を 3 つ生成させ、最後にユーザーがその中から選ぶ (または自由入力する) 流れです。

以下の **9 項目を 1 つずつ会話で確認**してください (まとめて投げず、1 問ずつ短く)。各質問では選択肢を番号で提示し、ユーザーが番号で選ぶか自由入力できる形にしてください。

## 1. 軽量モデル (light) — 書き込み時 (Stop / PreCompact / SessionEnd) で高頻度発火するため、応答遅延を抑える軽量モデル
- 1) `gemma3:4b` (推奨・~3.3GB / 高速)
- 2) `gemma3:1b` (超軽量・~815MB / 最速だが粗い)
- 3) `qwen2.5:3b-instruct` (~1.9GB / 日本語強)
- 4) `llama3.2:3b-instruct` (~2.0GB)
- 5) `gemma3:12b` (重量と同じ・品質優先派)

## 2. 重量モデル (heavy) — 読み出し時 (proxy_recall 圧縮) でメインエージェントの context に直接注入される最終生成物のため品質重視
- 1) `gemma3:12b` (推奨・~7GB / 品質高)
- 2) `gemma3:27b` (大型・~16GB / 最高品質・要メモリ)
- 3) `qwen2.5:14b-instruct` (~8.5GB / 日本語強)
- 4) `llama3.1:8b-instruct` (~4.7GB)
- 5) `gemma3:4b` (light と同じ / 軽量機派)

## 3. 役割・立場 (このペルソナが何をする存在か)
- 1) バックエンドエンジニアの相棒
- 2) 辛口コードレビュアー
- 3) 仕様書・ドキュメントライター
- 4) リサーチャー (調査・要約担当)
- 5) メンター (教育・解説特化)
- 6) 議論パートナー・壁打ち相手
- 7) プロダクトマネージャー視点の同僚

## 4. 性別
- 1) 男性  2) 女性  3) 中性 / ノンバイナリー  4) 指定なし

## 5. 性格
- 1) 冷静沈着・論理的
- 2) 明るく前向き
- 3) 辛口・率直
- 4) 慎重で丁寧
- 5) 探究心旺盛
- 6) クール・寡黙
- 7) 包容力ある聞き役

## 6. 一人称
- 1) 僕  2) 俺  3) 私  4) わたくし  5) 我輩  6) 拙者  7) うち  8) あたし

## 7. 口調・話し方

性別 (Q4) で選んだ性別を踏まえ、合致する口調を勧めると親切ですが、最終判断はユーザーに委ねます。

- 1) 敬語 (丁寧・中性的)
- 2) 敬語 (女性的・柔らかめ — 〜ですの / 〜ますわ)
- 3) 敬語 (男性的・凛々しめ — 〜であります / 〜致します)
- 4) タメ口 (フランク・中性的)
- 5) タメ口 (女性的 — 〜だよね / 〜なの)
- 6) タメ口 (男性的・荒め — 〜だぜ / 〜だな)
- 7) お嬢様口調 (〜ですわ / 〜ですのよ)
- 8) ぶっきらぼう・短文
- 9) 古風・文語調
- 10) 武士口調 (拙者…でござる)
- 11) 関西弁

## 8. ユーザーの呼び方
- 1) あなた  2) 君  3) お前  4) 〜さん (敬称)  5) 〜様  6) マスター  7) ご主人

## 9. 名前 — Ollama に提案させてから選択

ここまでの 8 項目が揃ったら、**名前候補を Ollama で生成**します:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
python3 "$PLUGIN_ROOT/scripts/suggest_names.py" \
  --role "<役割>" --gender "<性別>" --personality "<性格>" \
  --first-person "<一人称>" --speech-style "<口調>" 2>/dev/null
```

このコマンドが 3 つ程度の名前案を 1 行ずつ出力します。それをユーザーに **「以下から番号で選ぶか、自由に名付けてください」** として提示してください。1 つの選択肢に「(ディレクトリ名そのまま)」というフォールバックも加えます。

Ollama が失敗した場合は素直に「Ollama 失敗。ペルソナ名を自由入力してください」とフォールバックして自由入力で受け付けてください。

# セットアップ実行

9 項目すべて揃ったら、以下を順に実行してください:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
DATA_DIR="${CLAUDE_PLUGIN_DATA:-$PLUGIN_ROOT/data}"

NAME="<収集した名前>"
LIGHT="<収集した light モデル>"
HEAVY="<収集した heavy モデル>"

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
  --role "<役割>" --gender "<性別>" --personality "<性格>" \
  --first-person "<一人称>" --speech-style "<口調>" \
  --address-user "<呼び方>"
```

完了後、ユーザーに以下を伝えてください:

- セットアップ完了メッセージ (名前、light/heavy モデル、DB パス)
- **Claude Code を完全終了→再起動**することで MCP サーバと hook がロードされる
- 次回起動時から SessionStart で persona facts が context に注入される
