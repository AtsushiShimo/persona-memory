#!/bin/sh
# Stop hook for persona-memory.
# Fires after each assistant turn. Saves raw turns + asks the local judge
# model whether anything is worth remembering as a structured fact.
#
# 重要: 完全 detached 実行。Claude Code の turn boundary は即時 exit 0 で
# 解放され、heavy work (LLM 呼び出し / DB write / 埋め込み) は別プロセスで
# 走る。これで次ターン処理を待たせない。

SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME
SCRIPT="$SCRIPT_HOME/scripts/auto_persist.py"
TIMING_LOG="${PERSONA_TIMING_LOG:-/tmp/persona_hook_timings.log}"

[ -f "$SCRIPT" ] || exit 0

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

[ -x "${PERSONA_PYTHON:-}" ] || exit 0

# stdin (hook payload: session_id / transcript_path 等) を temp file に退避
INPUT_TMP=$(mktemp -t persona_stop_input.XXXXXX)
cat > "$INPUT_TMP"

# 計測ログ: 起動時刻
T0_NS=$(date +%s%N)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stop hook fired (detaching auto_persist)" >> "$TIMING_LOG"

# 完全 detach: setsid で session 切り離し、出力捨て、temp を最後に消す
(
  setsid "$PERSONA_PYTHON" "$SCRIPT" < "$INPUT_TMP" >/dev/null 2>&1
  T1_NS=$(date +%s%N)
  ELAPSED_MS=$(( (T1_NS - T0_NS) / 1000000 ))
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stop hook bg done in ${ELAPSED_MS} ms" >> "$TIMING_LOG"
  rm -f "$INPUT_TMP"
) </dev/null >/dev/null 2>&1 &

# 即時 exit 0 (turn boundary を解放)
exit 0
