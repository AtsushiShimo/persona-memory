#!/bin/sh
# Sourceable helper for persona-memory hook scripts.
#
# Resolves the active persona and exports the env vars that the hook's
# Python entry-point will read:
#   PERSONA_MEMORY_DB    — sqlite DB path
#   PERSONA_LIGHT_MODEL  — write-time judge / per-turn fact extraction
#   PERSONA_HEAVY_MODEL  — read-time recall compression (proxy_recall)
#   PERSONA_EMBED_MODEL  — embedding model
#   OLLAMA_HOST          — Ollama daemon
#   PERSONA_JUDGE_MODEL  — back-compat alias for write-side scripts
#   PERSONA_RECALL_COMPRESS_MODEL — read-side override for proxy_recall
#
# Lookup order (first match wins):
#   1. Plugin form: $CLAUDE_PLUGIN_DATA/{active-persona, personas/<name>/config.env}
#   2. Standalone:  $SCRIPT_HOME/data/{active-persona, <name>.config.env}
#
# Per rule/model_weight_policy:
#   - write side (high frequency, raw turns are safety net) → light model
#   - read side  (low frequency, output goes into main agent context) → heavy model
#
# Fail-open: this script never errors out. Missing config = sensible fallback.

# SCRIPT_HOME is expected to be set by the caller (the hook script).
# In plugin form it equals $CLAUDE_PLUGIN_ROOT; in standalone it's the repo root.
[ -n "${SCRIPT_HOME:-}" ] || SCRIPT_HOME="$(pwd)"

# Claude Code (as of v2.1.x) does NOT reliably pass CLAUDE_PLUGIN_DATA into
# the MCP server's launch environment, even though plugin hooks do receive it.
# Without it, the MCP server can't locate the persistent persona DB and
# write_fact / append_episode tools fail with "PERSONA_MEMORY_DB unset".
#
# Workaround: derive the canonical persistent data dir from the cache path.
# Plugin layout convention:
#   ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/   ← CLAUDE_PLUGIN_ROOT
#   ~/.claude/plugins/data/<marketplace>-<plugin>/              ← CLAUDE_PLUGIN_DATA
if [ -z "${CLAUDE_PLUGIN_DATA:-}" ] && [ -n "${SCRIPT_HOME:-}" ]; then
  case "$SCRIPT_HOME" in
    */plugins/cache/*/*/*)
      # Standard install path: ~/.claude/plugins/cache/<market>/<plugin>/<version>
      _plugin_dir="$(dirname "$SCRIPT_HOME")"          # cache/<market>/<plugin>
      _market_dir="$(dirname "$_plugin_dir")"          # cache/<market>
      _plugins_root="$(dirname "$(dirname "$_market_dir")")"  # ~/.claude/plugins
      _derived="$_plugins_root/data/$(basename "$_market_dir")-$(basename "$_plugin_dir")"
      if [ -d "$_derived" ]; then
        CLAUDE_PLUGIN_DATA="$_derived"
      fi
      ;;
  esac
fi

# Resolve the data dir. Plugin form gets a stable per-install location that
# survives plugin updates ($CLAUDE_PLUGIN_DATA); standalone uses repo/data.
if [ -n "${CLAUDE_PLUGIN_DATA:-}" ] && [ -d "$CLAUDE_PLUGIN_DATA" ]; then
  _PERSONA_DATA_DIR="$CLAUDE_PLUGIN_DATA"
elif [ -d "$SCRIPT_HOME/data" ]; then
  _PERSONA_DATA_DIR="$SCRIPT_HOME/data"
else
  _PERSONA_DATA_DIR=""
fi

# Resolve active persona name. Falls back to the legacy "test-persona" so
# pre-plugin installs keep working without a config file.
_ACTIVE_PERSONA=""
if [ -n "$_PERSONA_DATA_DIR" ] && [ -r "$_PERSONA_DATA_DIR/active-persona" ]; then
  _ACTIVE_PERSONA="$(head -n1 "$_PERSONA_DATA_DIR/active-persona" 2>/dev/null | tr -d '\r\n ')"
fi
[ -z "$_ACTIVE_PERSONA" ] && _ACTIVE_PERSONA="test-persona"

# Source persona-specific config if it exists. The two layouts let us
# coexist with both plugin (per-persona dir) and legacy (flat) installs.
if [ -n "$_PERSONA_DATA_DIR" ]; then
  for _cfg in \
    "$_PERSONA_DATA_DIR/personas/$_ACTIVE_PERSONA/config.env" \
    "$_PERSONA_DATA_DIR/$_ACTIVE_PERSONA.config.env"; do
    if [ -r "$_cfg" ]; then
      # shellcheck disable=SC1090
      . "$_cfg"
      break
    fi
  done
fi

# Apply hardcoded fallbacks (only for vars still unset after sourcing config).
export OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
export PERSONA_LIGHT_MODEL="${PERSONA_LIGHT_MODEL:-gemma3:4b}"
export PERSONA_HEAVY_MODEL="${PERSONA_HEAVY_MODEL:-gemma3:12b}"
export PERSONA_EMBED_MODEL="${PERSONA_EMBED_MODEL:-nomic-embed-text}"

# Resolve DB path if not pre-set: prefer per-persona dir, fall back to flat.
if [ -z "${PERSONA_MEMORY_DB:-}" ] && [ -n "$_PERSONA_DATA_DIR" ]; then
  if [ -f "$_PERSONA_DATA_DIR/personas/$_ACTIVE_PERSONA/persona.db" ]; then
    export PERSONA_MEMORY_DB="$_PERSONA_DATA_DIR/personas/$_ACTIVE_PERSONA/persona.db"
  else
    export PERSONA_MEMORY_DB="$_PERSONA_DATA_DIR/$_ACTIVE_PERSONA.db"
  fi
fi

# Aliases consumed by the existing Python entry-points.
# - PERSONA_JUDGE_MODEL is what auto_persist / persist_before_compact read.
# - PERSONA_RECALL_COMPRESS_MODEL is what proxy_recall reads.
export PERSONA_JUDGE_MODEL="${PERSONA_JUDGE_MODEL:-$PERSONA_LIGHT_MODEL}"
export PERSONA_RECALL_COMPRESS_MODEL="${PERSONA_RECALL_COMPRESS_MODEL:-$PERSONA_HEAVY_MODEL}"

# CRITICAL: variables sourced from config.env are *shell-local* unless we
# export them. Python child processes (MCP server, auto_persist.py, etc.)
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
