#!/bin/sh
# UserPromptSubmit hook for persona-memory.
# proxy_recall がローカル LLM で関連記憶を引き、additionalContext として注入。
# fail-open: 何かあれば exit 0 で素通し。

# 子セッション (claude_session_summary.py が起動した claude -p) では skip
[ -n "$PERSONA_SUMMARY_CHILD" ] && exit 0

SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME
SCRIPT="$SCRIPT_HOME/scripts/proxy_recall.py"
TIMING_LOG="${PERSONA_TIMING_LOG:-/tmp/persona_hook_timings.log}"

[ -f "$SCRIPT" ] || exit 0

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

[ -x "${PERSONA_PYTHON:-}" ] || exit 0

# 計測: hook 開始時刻
T0_NS=$(date +%s%N)

# proxy_recall を実行 (stdin はそのまま forward)
"$PERSONA_PYTHON" "$SCRIPT"
RC=$?

# 計測: hook 終了時刻
T1_NS=$(date +%s%N)
ELAPSED_MS=$(( (T1_NS - T0_NS) / 1000000 ))
echo "[$(date '+%Y-%m-%d %H:%M:%S')] UserPromptSubmit hook ${ELAPSED_MS} ms (rc=$RC)" >> "$TIMING_LOG"

exit $RC
