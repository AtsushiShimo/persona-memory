#!/bin/sh
# Wrapper that launches the persona-memory MCP server with the active
# persona's env resolved. Used as the `command` entry in the plugin's
# .mcp.json so the same install can serve multiple personas — switching is
# done via /persona-memory:switch which updates active-persona and signals
# Claude Code to reload MCP servers.
#
# Standalone (non-plugin) installs can also point .mcp.json at this script
# instead of hard-coding env vars.

set -e

# Plugin form: $CLAUDE_PLUGIN_ROOT is set by Claude Code at launch time.
# Standalone form: derive from this script's location.
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then
  SCRIPT_HOME="$CLAUDE_PLUGIN_ROOT"
else
  SCRIPT_HOME="$(cd "$(dirname "$0")/.." && pwd)"
fi
export SCRIPT_HOME

# shellcheck disable=SC1091
. "$SCRIPT_HOME/scripts/load_persona_env.sh"

if [ ! -x "${PERSONA_PYTHON:-}" ]; then
  echo "persona-memory: python venv not found. Run /persona-memory:init first." >&2
  exit 1
fi

cd "$SCRIPT_HOME"
exec "$PERSONA_PYTHON" -m server.main
