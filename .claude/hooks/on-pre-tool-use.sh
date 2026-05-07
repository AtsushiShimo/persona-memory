#!/bin/sh
# PreToolUse hook — DB 直接アクセスをブロック.

[ -n "$PERSONA_SUMMARY_CHILD" ] && exit 0
[ -n "$PERSONA_ESCALATION_CHILD" ] && exit 0

SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

[ -x "${PERSONA_PYTHON:-}" ] || exit 0

PYTHONPATH="$SCRIPT_HOME${PYTHONPATH:+:$PYTHONPATH}" \
  "$PERSONA_PYTHON" -m scripts.hooks.on_pre_tool_use
