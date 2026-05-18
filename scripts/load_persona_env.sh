#!/bin/sh
# Sourceable helper for persona-memory hook scripts.
#
# Resolves the active persona and exports the env vars that the hook's
# Python entry-point will read:
#   PERSONA_MEMORY_DB    — Cozo DB path (`<persona>.cozo.db`)
#   PERSONA_LIGHT_MODEL  — write-time fact extraction (write LLM)
#   PERSONA_HEAVY_MODEL  — read-time recall summarize / discussion extract
#   PERSONA_EMBED_MODEL  — embedding model
#   OLLAMA_HOST          — Ollama daemon
#   PERSONA_JUDGE_MODEL  — back-compat alias for write-side scripts
#   PERSONA_RECALL_COMPRESS_MODEL — back-compat alias (heavy model)
#
# Lookup order (project-local only):
#   $CLAUDE_PROJECT_DIR/.persona-memory/{active-persona, <name>.config.env}
#
# Per rule/model_weight_policy:
#   - write side (high frequency, raw turns are safety net) → light model
#   - read side  (low frequency, output goes into main agent context) → heavy model
#
# Fail-open: this script never errors out. Missing config = sensible fallback.

# SCRIPT_HOME is expected to be set by the caller (the hook script).
# In plugin form it equals $CLAUDE_PLUGIN_ROOT; in standalone it's the repo root.
[ -n "${SCRIPT_HOME:-}" ] || SCRIPT_HOME="$(pwd)"

# Project dir = where the user runs `claude`. Each project has its own
# persona memory under <project>/.persona-memory/ so different projects
# are different agents with different histories. Hooks receive
# CLAUDE_PROJECT_DIR; otherwise fall back to pwd.
_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# Per-project memory location. DB / active-persona / config.env live here.
_PERSONA_DATA_DIR="$_PROJECT_DIR/.persona-memory"

# Plugin venv lives globally in CLAUDE_PLUGIN_DATA (shared across projects).
# Derive it if Claude Code didn't pass CLAUDE_PLUGIN_DATA (it's intermittent).
# Convention:
#   ~/.claude/plugins/cache/<market>/<plugin>/<version>/   ← CLAUDE_PLUGIN_ROOT
#   ~/.claude/plugins/data/<market>-<plugin>/              ← CLAUDE_PLUGIN_DATA
if [ -z "${CLAUDE_PLUGIN_DATA:-}" ] && [ -n "${SCRIPT_HOME:-}" ]; then
  case "$SCRIPT_HOME" in
    */plugins/cache/*/*/*)
      _plugin_dir="$(dirname "$SCRIPT_HOME")"
      _market_dir="$(dirname "$_plugin_dir")"
      _plugins_root="$(dirname "$(dirname "$_market_dir")")"
      _derived="$_plugins_root/data/$(basename "$_market_dir")-$(basename "$_plugin_dir")"
      if [ -d "$_derived" ]; then
        CLAUDE_PLUGIN_DATA="$_derived"
      fi
      ;;
  esac
fi

# Resolve active persona name from project's persona memory dir.
_ACTIVE_PERSONA=""
if [ -r "$_PERSONA_DATA_DIR/active-persona" ]; then
  _ACTIVE_PERSONA="$(head -n1 "$_PERSONA_DATA_DIR/active-persona" 2>/dev/null | tr -d '\r\n ')"
fi

# If no project-local persona is initialized, leave variables empty so the
# hook scripts gracefully no-op (fail-open). The MCP server will report
# "DB not initialized" and direct the user to /persona-memory:init.
if [ -n "$_ACTIVE_PERSONA" ] && [ -r "$_PERSONA_DATA_DIR/$_ACTIVE_PERSONA.config.env" ]; then
  # shellcheck disable=SC1090
  . "$_PERSONA_DATA_DIR/$_ACTIVE_PERSONA.config.env"
fi

# Apply hardcoded fallbacks (only for vars still unset after sourcing config).
export OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
export PERSONA_LIGHT_MODEL="${PERSONA_LIGHT_MODEL:-gemma3:4b}"
export PERSONA_HEAVY_MODEL="${PERSONA_HEAVY_MODEL:-gemma3:12b}"
export PERSONA_EMBED_MODEL="${PERSONA_EMBED_MODEL:-nomic-embed-text}"

# Resolve DB path if config.env didn't set it: project-local <persona>.cozo.db.
# 旧 SQLite path (`<persona>.db`) しか手元に無い古い config.env は upgrade で
# in-place migrate されるため、 ここでは Cozo path のみを fallback として見る.
if [ -z "${PERSONA_MEMORY_DB:-}" ] && [ -n "$_ACTIVE_PERSONA" ]; then
  if [ -f "$_PERSONA_DATA_DIR/$_ACTIVE_PERSONA.cozo.db" ]; then
    export PERSONA_MEMORY_DB="$_PERSONA_DATA_DIR/$_ACTIVE_PERSONA.cozo.db"
  fi
fi

# Aliases consumed by Python entry-points (back-compat with pre-0.8 names).
export PERSONA_JUDGE_MODEL="${PERSONA_JUDGE_MODEL:-$PERSONA_LIGHT_MODEL}"
export PERSONA_RECALL_COMPRESS_MODEL="${PERSONA_RECALL_COMPRESS_MODEL:-$PERSONA_HEAVY_MODEL}"

# CRITICAL: variables sourced from config.env are *shell-local* unless we
# export them. Python child processes (MCP server, write/run.py, etc.)
# only see the environment, not the shell scope. Without these explicit
# exports, PERSONA_MEMORY_DB is invisible to Python and the MCP server's
# write_fact / append_episode tools fail with "PERSONA_MEMORY_DB unset".
export PERSONA_MEMORY_DB
export PERSONA_LIGHT_MODEL
export PERSONA_HEAVY_MODEL
export PERSONA_EMBED_MODEL
export OLLAMA_HOST
export PERSONA_JUDGE_MODEL
export PERSONA_RECALL_COMPRESS_MODEL

# Resolve which python to use. The shared venv lives under
# $CLAUDE_PLUGIN_DATA/.venv (created once by /persona-memory:init via
# setup.sh) and is reused across all projects on the same machine.
# Standalone setups can also fall back to SCRIPT_HOME/.venv.
if [ -n "${CLAUDE_PLUGIN_DATA:-}" ] && [ -x "$CLAUDE_PLUGIN_DATA/.venv/bin/python" ]; then
  export PERSONA_PYTHON="$CLAUDE_PLUGIN_DATA/.venv/bin/python"
elif [ -x "$SCRIPT_HOME/.venv/bin/python" ]; then
  export PERSONA_PYTHON="$SCRIPT_HOME/.venv/bin/python"
fi
