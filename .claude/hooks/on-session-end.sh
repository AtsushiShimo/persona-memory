#!/bin/sh
# SessionEnd hook for persona-memory.
# Fires when a session ends (including /clear and explicit exits). PreCompact
# only fires on auto/manual compact, so without this hook anything discussed
# in a short session — or wiped via /clear — never reaches long-term memory.
#
# Reuses persist_before_compact.py; PERSONA_SNAPSHOT_KIND switches the label.
# Per rule/model_weight_policy: write path → light model.
# Fail-open: any error -> exit 0 silently so the session shutdown proceeds.

SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME
PYTHON="$SCRIPT_HOME/.venv/bin/python"
SCRIPT="$SCRIPT_HOME/scripts/persist_before_compact.py"

[ -x "$PYTHON" ] || exit 0
[ -f "$SCRIPT" ] || exit 0

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"
export PERSONA_SNAPSHOT_KIND="SessionEnd"

exec "$PYTHON" "$SCRIPT"
