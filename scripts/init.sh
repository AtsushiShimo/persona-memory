#!/usr/bin/env bash
# Interactive bootstrap for a freshly cloned persona-memory (redesign edition).
#
# Asks 9 questions and seeds the persona's foundational identity into the
# new schema as 'persona' category facts (= boot 層, SessionStart で常時注入).
#
# 質問順:
#   0a. 軽量モデル (write 側 — fact 抽出用、高頻度発火)
#   0b. 重量モデル (read 側 — recall キーワード抽出 + 圧縮、品質重視)
#   1.  役割 (このペルソナの存在意義 — 最重要)
#   2.  性別
#   3.  性格
#   4.  一人称
#   5.  口調・話し方
#   6.  ユーザーの呼び方
#   7.  名前 (最後に Ollama で 3 案を動的生成)
#
# Usage: scripts/init.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ask_default() {
  local label="$1" default="$2" answer=""
  read -r -p "$label [$default]: " answer
  printf '%s' "${answer:-$default}"
}

ask_choice() {
  local label="$1"; shift
  local options=("$@")
  local n=${#options[@]}
  local answer=""
  while [[ -z "$answer" ]]; do
    {
      printf '%s\n' "$label"
      local i=1
      for opt in "${options[@]}"; do
        printf '  %d) %s\n' "$i" "$opt"
        i=$((i+1))
      done
    } >&2
    read -r -p "  選択 (1-$n) または自由入力: " answer
    if [[ "$answer" =~ ^[0-9]+$ ]] && (( answer >= 1 && answer <= n )); then
      answer="${options[$((answer-1))]}"
    fi
  done
  printf '%s' "$answer"
}

cat <<'BANNER'
=========================================================
 persona-memory: 初期セットアップ (redesign)
=========================================================
 ローカル LLM (Ollama) のモデル 2 種を選び、続けて 7 つの
 ペルソナ質問に答えてください。各項目は番号で選ぶか自由
 入力できます。

 モデル方針:
   light = 書き込み (write LLM) = 高頻度。応答遅延を抑える
           ため軽量。生ターンが救命網。
   heavy = 読み出し (recall LLM) = キーワード抽出 + 検索結果
           整形。Claude Code の context に注入されるため品質
           重視。
---------------------------------------------------------
BANNER

DEFAULT_NAME="$(basename "$ROOT")"

# 0a. 軽量モデル (write 側)
if [[ -z "${PERSONA_LIGHT_MODEL:-}" ]]; then
  LIGHT_MODEL=$(ask_choice "[0a/9] 軽量モデル (write・高頻度発火)" \
    "gemma3:4b (推奨・~3.3GB / 高速)" \
    "gemma3:1b (超軽量・~815MB / 最速だが粗い)" \
    "qwen2.5:3b-instruct (~1.9GB / 日本語強・軽量)" \
    "llama3.2:3b-instruct (~2.0GB)" \
    "gemma3:12b (重量と同じ・~7GB / 品質優先派向け)")
  LIGHT_MODEL="${LIGHT_MODEL%% (*}"
else
  LIGHT_MODEL="$PERSONA_LIGHT_MODEL"
  echo "[0a/9] light model: $LIGHT_MODEL (env で指定済み)" >&2
fi

# 0b. 重量モデル (read 側)
if [[ -z "${PERSONA_HEAVY_MODEL:-}" ]]; then
  HEAVY_MODEL=$(ask_choice "[0b/9] 重量モデル (recall・品質重視)" \
    "gemma3:12b (推奨・~7GB / 品質高)" \
    "gemma3:27b (大型・~16GB / 最高品質・要メモリ)" \
    "qwen2.5:14b-instruct (~8.5GB / 日本語強)" \
    "llama3.1:8b-instruct (~4.7GB)" \
    "gemma3:4b (light と同じ・~3.3GB / 軽量機派向け)")
  HEAVY_MODEL="${HEAVY_MODEL%% (*}"
else
  HEAVY_MODEL="$PERSONA_HEAVY_MODEL"
  echo "[0b/9] heavy model: $HEAVY_MODEL (env で指定済み)" >&2
fi

EMBED_MODEL="${PERSONA_EMBED_MODEL:-nomic-embed-text}"

export PERSONA_LIGHT_MODEL="$LIGHT_MODEL"
export PERSONA_HEAVY_MODEL="$HEAVY_MODEL"
export PERSONA_EMBED_MODEL="$EMBED_MODEL"
# suggest_names.py は PERSONA_JUDGE_MODEL を読むので alias を渡す。
export PERSONA_JUDGE_MODEL="$LIGHT_MODEL"

# 1. 役割
PERSONA_ROLE=$(ask_choice "[1/9] 役割・立場 (このペルソナが何をする存在か)" \
  "バックエンドエンジニアの相棒" \
  "辛口コードレビュアー" \
  "仕様書・ドキュメントライター" \
  "リサーチャー (調査・要約担当)" \
  "メンター (教育・解説特化)" \
  "議論パートナー・壁打ち相手" \
  "プロダクトマネージャー視点の同僚")

# 2. 性別
PERSONA_GENDER=$(ask_choice "[2/9] 性別" \
  "男性" \
  "女性" \
  "中性 / ノンバイナリー" \
  "指定なし")

# 3. 性格
PERSONA_PERSONALITY=$(ask_choice "[3/9] 性格" \
  "冷静沈着・論理的" \
  "明るく前向き" \
  "辛口・率直" \
  "慎重で丁寧" \
  "探究心旺盛" \
  "クール・寡黙" \
  "包容力ある聞き役")

# 4. 一人称
PERSONA_FIRST_PERSON=$(ask_choice "[4/9] 一人称" \
  "僕" \
  "俺" \
  "私" \
  "わたくし" \
  "我輩" \
  "拙者" \
  "うち" \
  "あたし")

# 5. 口調
PERSONA_SPEECH=$(ask_choice "[5/9] 口調・話し方" \
  "敬語 (丁寧・中性的)" \
  "敬語 (女性的・柔らかめ — 〜ですの/〜ますわ)" \
  "敬語 (男性的・凛々しめ — 〜であります/〜致します)" \
  "タメ口 (フランク・中性的)" \
  "タメ口 (女性的 — 〜だよね/〜なの)" \
  "タメ口 (男性的・荒め — 〜だぜ/〜だな)" \
  "お嬢様口調 (〜ですわ/〜ですのよ)" \
  "ぶっきらぼう・短文" \
  "古風・文語調" \
  "武士口調 (拙者…でござる)" \
  "関西弁")

# 6. ユーザーの呼び方
PERSONA_ADDRESS=$(ask_choice "[6/9] ユーザーの呼び方" \
  "あなた" \
  "君" \
  "お前" \
  "〜さん (敬称)" \
  "〜様" \
  "マスター" \
  "ご主人")

# 7. 名前 — Ollama に提案させる
echo >&2
echo "[7/9] ここまでの設定から名前候補を生成中... (Ollama)" >&2
SUGGESTED=()
while IFS= read -r line; do
  [[ -n "$line" ]] && SUGGESTED+=("$line")
done < <(python3 "$ROOT/scripts/suggest_names.py" \
  --role "$PERSONA_ROLE" \
  --gender "$PERSONA_GENDER" \
  --personality "$PERSONA_PERSONALITY" \
  --first-person "$PERSONA_FIRST_PERSON" \
  --speech-style "$PERSONA_SPEECH" 2>/dev/null || true)

if [[ ${#SUGGESTED[@]} -gt 0 ]]; then
  PERSONA_NAME=$(ask_choice "[7/9] ペルソナの名前 (生成案または自由入力)" \
    "${SUGGESTED[@]}" \
    "$DEFAULT_NAME (ディレクトリ名そのまま)")
  PERSONA_NAME="${PERSONA_NAME%% (ディレクトリ名そのまま)}"
else
  echo "  (生成失敗または Ollama 未起動。フォールバックで自由入力)" >&2
  PERSONA_NAME=$(ask_default "[7/9] ペルソナの名前" "$DEFAULT_NAME")
fi

# --- venv + Ollama models + DB (setup.sh が .persona-memory/ 配下に作る) ---
"$ROOT/setup.sh" "$PERSONA_NAME"

# --- パス確定 (load_persona_env.sh と同じ規約) ---
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
PERSONA_DIR="$PROJECT_DIR/.persona-memory"
DB_PATH="$PERSONA_DIR/$PERSONA_NAME.cozo.db"
CONFIG_ENV="$PERSONA_DIR/$PERSONA_NAME.config.env"

# --- write per-persona config.env ---
# DB path is written with a literal ${CLAUDE_PROJECT_DIR} so the project root
# is resolved at source-time. This keeps the config portable when the project
# directory is moved or copied.
cat > "$CONFIG_ENV" <<EOF
# Generated by scripts/init.sh on $(date '+%Y-%m-%d %H:%M:%S')
# Sourced by .claude/hooks/* via scripts/load_persona_env.sh
PERSONA_MEMORY_DB="\${CLAUDE_PROJECT_DIR:-\$(pwd)}/.persona-memory/$PERSONA_NAME.cozo.db"
PERSONA_LIGHT_MODEL="$LIGHT_MODEL"
PERSONA_HEAVY_MODEL="$HEAVY_MODEL"
PERSONA_EMBED_MODEL="$EMBED_MODEL"
OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
EOF
echo "[ok] $CONFIG_ENV を生成しました"

# Mark this persona as active so hooks pick up its config automatically.
echo "$PERSONA_NAME" > "$PERSONA_DIR/active-persona"
echo "[ok] active-persona = $PERSONA_NAME"

# --- seed initial persona facts ---
export PERSONA_MEMORY_DB="$DB_PATH"
PYTHONPATH="$ROOT" "$ROOT/.venv/bin/python" "$ROOT/scripts/seed_persona.py" \
  --db "$DB_PATH" \
  --role "$PERSONA_ROLE" \
  --name "$PERSONA_NAME" \
  --gender "$PERSONA_GENDER" \
  --personality "$PERSONA_PERSONALITY" \
  --first-person "$PERSONA_FIRST_PERSON" \
  --speech-style "$PERSONA_SPEECH" \
  --address-user "$PERSONA_ADDRESS"

cat <<EOF

=========================================================
 セットアップ完了: $PERSONA_NAME
=========================================================
  役割:        $PERSONA_ROLE
  性格:        $PERSONA_PERSONALITY
  口調:        $PERSONA_SPEECH
  DB:          $DB_PATH
  light モデル: $LIGHT_MODEL  (write 側)
  heavy モデル: $HEAVY_MODEL  (recall 側)
  config.env:  $CONFIG_ENV
  active 印:   $PERSONA_DIR/active-persona

 次のステップ:
   claude     # Claude Code をこのディレクトリで起動

 SessionStart で boot 層 (persona facts) が自動注入されます。
 デバッグログを見たい場合は:
   export PERSONA_MEMORY_DEBUG=c  # default: 最詳細
=========================================================
EOF
