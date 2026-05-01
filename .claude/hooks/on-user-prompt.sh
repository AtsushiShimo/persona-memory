#!/bin/sh
# UserPromptSubmit hook for persona-memory.
# Calls the local-LLM proxy to fetch relevant memories and inject them as
# additionalContext, so the agent sees recall hints aligned with the user's
# current message — without paying the token cost of a full memory dump.
#
# Per rule/model_weight_policy: read path → heavy model (only fires on large
# recall sets via proxy_recall.compress_episodes).
#
# Designed to fail open: if anything goes wrong (Ollama down, DB missing),
# exit 0 with no output and let the prompt pass through unchanged.

SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME
SCRIPT="$SCRIPT_HOME/scripts/proxy_recall.py"

[ -f "$SCRIPT" ] || exit 0

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

[ -x "${PERSONA_PYTHON:-}" ] || exit 0

# Hook stdin is forwarded as-is to the recall script.
exec "$PERSONA_PYTHON" "$SCRIPT"
