#!/bin/sh
# SessionStart hook for persona-memory.
# Injects:
#   1. 'persona' category facts (= 性格 / 対話スタイル / 振る舞いルール) — always
#   2. Recent active 'context' facts (importance >= 4)             — NEW
#   3. Latest N episode summaries                                  — NEW
# Items 2-3 give the agent immediate awareness of "what we worked on recently"
# without waiting for the user's first prompt to trigger proxy recall.
# Other categories (preference / rule / profile / skill) stay on dynamic
# recall via the UserPromptSubmit hook to keep startup tokens reasonable.

set -e

# 子セッション (claude_session_summary.py が起動した claude -p) では skip
[ -n "$PERSONA_SUMMARY_CHILD" ] && exit 0

# Resolve the plugin root (or repo root when running standalone). All script
# paths are relative to this so the hook works in either form.
SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME
cd "$SCRIPT_HOME" || exit 0

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

DB_PATH="$PERSONA_MEMORY_DB"
SQLITE="/opt/homebrew/opt/sqlite/bin/sqlite3"
[ -x "$SQLITE" ] || SQLITE="sqlite3"
PYTHON="${PERSONA_PYTHON:-}"

if [ ! -f "$DB_PATH" ]; then
  printf 'persona-memory DB not initialized yet at %s — run setup.sh first.\n' "$DB_PATH"
  exit 0
fi

# プラグイン共通の default 行動指針を auto-seed (既存 install の self-heal)。
# 同じ category/key で既に値があれば INSERT OR IGNORE で何もしない。
# importance=9 で SessionStart 時に必ず注入される。ユーザーが override したい
# 場合は write_fact で同じ key に上書きすれば差し替わる。
"$SQLITE" "$DB_PATH" >/dev/null 2>&1 <<'SQL' || true
INSERT OR IGNORE INTO facts(category, key, value, importance, source, created_at, updated_at)
VALUES (
  'persona',
  'response_brevity',
  '応答は端的に。質問に対しては核だけ即答する。前置き・状況再確認・『ご質問の件ですが』等の枕詞を省く。長文は禁止、必要なら 1-2 行の補足のみ。複数案を並べるのは明示的に求められた時だけ。理由: 長い応答は読む手間とトークン課金を増やす。',
  9,
  'plugin_default_v1',
  datetime('now', '+9 hours'),
  datetime('now', '+9 hours')
);
INSERT OR IGNORE INTO facts(category, key, value, importance, source, created_at, updated_at)
VALUES (
  'persona',
  'confirmation_before_acting',
  'ユーザーが疑問形 (『〜してみる？』『どうする？』『〜できる？』等) で問いかけた場合、それは提案であって指示ではない。ユーザーの明示的な承認 (『はい』『お願い』『進めて』『やって』等) を待ってから実行する。承認なしに勝手に始めない。推奨や対案を提示した後も同じ — ユーザーの選択を待つ。例外: typo 修正のような自明な瑣末な作業、同セッションで既に承認済みの繰り返し作業。理由: 勝手に始めると時間・計算コストが無駄になり、ユーザーの意図と逸れる。',
  9,
  'plugin_default_v2',
  datetime('now', '+9 hours'),
  datetime('now', '+9 hours')
);
SQL

PERSONA=$("$SQLITE" "$DB_PATH" <<'SQL' 2>/dev/null
.mode list
.separator "|"
SELECT key, value, importance, updated_at
FROM facts
WHERE status = 'active' AND category = 'persona'
ORDER BY importance DESC, updated_at DESC
LIMIT 30;
SQL
)

CONFLICTS=$("$SQLITE" "$DB_PATH" <<'SQL' 2>/dev/null
.mode list
.separator "|"
SELECT c.confidence,
       fa.category || '/' || fa.key,
       fa.value,
       fb.value
FROM conflicts c
JOIN facts fa ON fa.id = c.fact_a_id
JOIN facts fb ON fb.id = c.fact_b_id
WHERE c.resolution = 'pending'
ORDER BY c.detected_at DESC
LIMIT 5;
SQL
)

# Recent project state — active 'context' facts with non-trivial importance.
RECENT_CONTEXT=$("$SQLITE" "$DB_PATH" <<'SQL' 2>/dev/null
.mode list
.separator "|"
SELECT key, importance, updated_at, substr(value, 1, 200)
FROM facts
WHERE status = 'active' AND category = 'context' AND importance >= 4
ORDER BY updated_at DESC
LIMIT 5;
SQL
)

# Recent episodes — what was discussed/decided recently.
# 書き込み時要約は廃止 (rule/memory_save_policy)。生発話の content を
# 冒頭 240 字だけ表示する形に切替。raw_user/raw_assistant/raw_dump 系を
# 対象、session-start マーカーは除外。
RECENT_EPISODES=$("$SQLITE" "$DB_PATH" <<'SQL' 2>/dev/null
.mode list
.separator "|"
SELECT created_at, substr(content, 1, 240)
FROM episodes
WHERE content IS NOT NULL AND content != ''
  AND role IN ('raw_user', 'raw_assistant', 'raw_dump')
ORDER BY created_at DESC
LIMIT 5;
SQL
)

# Generate a session-start greeting using the judge model.
# Piped from RECENT_EPISODES so no extra DB query; fail-open (empty = skip).
GREETING=""
GEN_SCRIPT="$REPO_ROOT/scripts/gen_greeting.py"
if [ -n "$RECENT_EPISODES" ] && [ -x "$PYTHON" ] && [ -f "$GEN_SCRIPT" ]; then
  EPISODE_TEXT=$(printf '%s\n' "$RECENT_EPISODES" | awk -F'|' '{print $1 ": " $2}')
  # gen_greeting reads PERSONA_LIGHT_MODEL via PERSONA_JUDGE_MODEL alias
  # (set by load_persona_env.sh). It's a quick one-liner generation, so
  # the light model is appropriate even though greeting is read-side.
  GREETING=$(printf '%s\n' "$EPISODE_TEXT" | \
    "$PYTHON" "$GEN_SCRIPT" 2>/dev/null || true)
fi

{
  printf '=== persona-memory: 起動時人格注入 (%s) ===\n' "$(date '+%Y-%m-%d %H:%M')"
  printf 'DB: %s\n\n' "$DB_PATH"

  if [ -z "$PERSONA" ]; then
    printf 'まだ persona (性格・対話スタイル) facts は登録されていません。\n'
    printf '会話の中でユーザーから振る舞いに関する指示があれば、確認なしで\n'
    printf 'write_fact(category="persona", ...) で記録してください。\n'
  else
    printf '## persona (性格・対話スタイル)\n\n'
    printf '%s\n' "$PERSONA" | awk -F'|' '{
      printf "- [persona/%s] (importance=%s, updated=%s)\n  %s\n", $1, $3, $4, $2
    }'
  fi

  if [ -n "$CONFLICTS" ]; then
    printf '\n## 未解決の矛盾 (要確認)\n\n'
    printf '%s\n' "$CONFLICTS" | awk -F'|' '{
      printf "- [confidence=%s] %s\n  A: %s\n  B: %s\n", $1, $2, $3, $4
    }'
  fi

  if [ -n "$RECENT_CONTEXT" ]; then
    printf '\n## 直近のプロジェクト状況 (context facts)\n\n'
    printf '%s\n' "$RECENT_CONTEXT" | awk -F'|' '{
      printf "- [context/%s] (importance=%s, updated=%s)\n  %s\n", $1, $2, $3, $4
    }'
  fi

  if [ -n "$RECENT_EPISODES" ]; then
    printf '\n## 直近の議論ログ (recent episodes)\n\n'
    printf '%s\n' "$RECENT_EPISODES" | awk -F'|' '{
      printf "- [%s] %s\n", $1, $2
    }'
  fi

  if [ -n "$GREETING" ]; then
    printf '\n## 起動挨拶\n'
    printf '最初のユーザー発話への返答の冒頭に、以下の一文を自然に含めてください:\n'
    printf '%s\n' "$GREETING"
  fi

  printf '\n## 行動指針\n'
  printf 'persona 以外の関連記憶は UserPromptSubmit 時に proxy が自動付与します。\n'
  printf 'recall に出ない情報を深掘りしたい時は **MCP の search_memory ツール** を呼ぶ。\n'
  printf '**禁止**: Bash + sqlite3 で DB を直接覗く動作 (UI に Bash 出力が残ってノイズになる)。\n'
  printf '同じ理由で `Bash(ls .../persona-memory/...)` 等で plugin 内部を漁るのも避ける。\n'
  printf 'DB に何があるかを知りたければ search_memory / list_facts MCP ツールで済ませる。\n'
  printf '会話で出てきた preference/rule/profile/skill/context/persona は確認なしで write_fact してください。\n'
  printf '振る舞い指示 (例: "確認時は根拠と選択肢を出して") は category="persona" で書いてください。\n'
} 2>&1

# Persist a "session started" episode so the boundary itself is in the DB
# (auto_persist only fires after assistant turns and skips system messages).
# NOTE: `grep -c .` exits 1 when input is empty. Combined with `set -e`
# at top of file, that would silently kill the whole hook on a fresh DB
# (no context facts / episodes yet). `|| true` keeps the captured value
# ("0") while suppressing the non-zero exit.
PERSONA_COUNT=$(printf '%s\n' "$PERSONA"        | grep -c . || true)
CONTEXT_COUNT=$(printf '%s\n' "$RECENT_CONTEXT" | grep -c . || true)
EPISODE_COUNT=$(printf '%s\n' "$RECENT_EPISODES"| grep -c . || true)
TOPIC_HINT=$(printf '%s\n' "$RECENT_EPISODES" | head -3 | awk -F'|' '{
  s = $2; gsub(/[\r\n]+/, " ", s); print substr(s, 1, 90)
}' | paste -sd '; ' -)
SUMMARY_LINE=$(printf 'Session started at %s. Injected: persona=%s, context=%s, episodes=%s. Recent topics: %s' \
  "$(date '+%Y-%m-%d %H:%M')" "$PERSONA_COUNT" "$CONTEXT_COUNT" "$EPISODE_COUNT" "$TOPIC_HINT")

LOGGER="$SCRIPT_HOME/scripts/log_session_start.py"
if [ -x "$PYTHON" ] && [ -f "$LOGGER" ]; then
  # All env vars are already exported by load_persona_env.sh.
  printf '%s\n' "$SUMMARY_LINE" | "$PYTHON" "$LOGGER" >/dev/null 2>&1 &
fi
