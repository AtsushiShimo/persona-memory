#!/bin/sh
# Stop hook for persona-memory.
# Fires after each assistant turn. The Python script saves raw turns
# unconditionally and asks the local judge model whether anything in the
# latest slice is worth remembering as a structured fact.
#
# Per rule/model_weight_policy: this is the *write* path → light model.
# Per rule/memory_save_policy: raw turns are saved before judge runs, so
# even if the judge fails the conversation is preserved.
#
# Fail-open: any error -> exit 0 silently so Stop processing proceeds.

SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME
SCRIPT="$SCRIPT_HOME/scripts/auto_persist.py"

[ -f "$SCRIPT" ] || exit 0

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

[ -x "${PERSONA_PYTHON:-}" ] || exit 0

exec "$PERSONA_PYTHON" "$SCRIPT"
