#!/bin/sh
# SessionEnd hook (0.6.24):
# - 当該 session の topic_id を解決し、 title + summary を 1 LLM コールで生成.
# - PERSONA_TOPIC_DISABLE=1 / PERSONA_TOPIC_SUMMARY_DISABLE=1 で skip.
# - fail-open: 例外は呑んで exit 0.

[ -n "$PERSONA_SUMMARY_CHILD" ] && exit 0
[ -n "$PERSONA_ESCALATION_CHILD" ] && exit 0

SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

[ -x "${PERSONA_PYTHON:-}" ] || exit 0

PYTHONPATH="$SCRIPT_HOME${PYTHONPATH:+:$PYTHONPATH}" \
  "$PERSONA_PYTHON" -m scripts.hooks.on_session_end || exit 0
