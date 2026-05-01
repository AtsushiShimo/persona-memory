#!/bin/sh
# PreCompact hook for persona-memory.
# Snapshots the current conversation as both a raw_dump episode (verbatim
# turns) and a summary episode just before Claude Code compacts the context.
#
# Per rule/model_weight_policy: write path → light model.
#
# Fail-open: any error -> exit 0 silently so compaction proceeds normally.

SCRIPT_HOME="${CLAUDE_PLUGIN_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
export SCRIPT_HOME
PYTHON="$SCRIPT_HOME/.venv/bin/python"
SCRIPT="$SCRIPT_HOME/scripts/persist_before_compact.py"

[ -x "$PYTHON" ] || exit 0
[ -f "$SCRIPT" ] || exit 0

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

exec "$PYTHON" "$SCRIPT"
