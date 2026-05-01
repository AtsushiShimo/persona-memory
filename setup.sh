#!/usr/bin/env bash
# Set up a persona memory MCP server: venv + deps + Ollama models + DB.
#
# Usage:  ./setup.sh <persona-name>
# Example: ./setup.sh methodology-explorer
#
# Plugin form: invoked indirectly via /persona-memory:init. Writes .venv and
# DB into $CLAUDE_PLUGIN_DATA so plugin version bumps (which wipe the cache
# tree) do NOT destroy the python venv or the persisted facts.
#
# Standalone form: invoked from a cloned repo. Writes .venv into $ROOT/.venv
# and DB into $ROOT/data/<persona>.db (legacy layout).

set -euo pipefail

PERSONA="${1:-}"
if [[ -z "$PERSONA" ]]; then
  echo "Usage: $0 <persona-name>" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve the persistent data dir. In plugin form Claude Code passes
# CLAUDE_PLUGIN_DATA (eg ~/.claude/plugins/data/<market>-<plugin>); we use
# that as the home for both .venv and the SQLite DB so they survive plugin
# version bumps. In standalone form we fall back to $ROOT.
if [[ -n "${CLAUDE_PLUGIN_DATA:-}" ]]; then
  DATA_DIR="$CLAUDE_PLUGIN_DATA"
else
  DATA_DIR="$ROOT/data"
fi
mkdir -p "$DATA_DIR"

DB_PATH="$DATA_DIR/$PERSONA.db"
VENV="$DATA_DIR/.venv"
EMBED_MODEL="${PERSONA_EMBED_MODEL:-nomic-embed-text}"
JUDGE_MODEL="${PERSONA_JUDGE_MODEL:-gemma3:12b}"

log() { printf '\033[36m[setup]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[setup]\033[0m %s\n' "$*" >&2; }

# 1. venv + deps (in $DATA_DIR/.venv so it persists across plugin updates)
if [[ ! -d "$VENV" ]]; then
  log "creating venv at $VENV"
  python3 -m venv "$VENV"
fi
log "installing Python deps"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "mcp[cli]" sqlite-vec httpx

# 2. Ollama check + model pull
if ! command -v ollama >/dev/null 2>&1; then
  err "ollama not found in PATH. Install from https://ollama.com/download then re-run."
  exit 2
fi
if ! curl -sS --max-time 2 http://localhost:11434/api/tags >/dev/null; then
  log "ollama daemon not running; starting in background"
  ollama serve >/dev/null 2>&1 &
  sleep 2
fi
for m in "$EMBED_MODEL" "$JUDGE_MODEL"; do
  if ! ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$m\(:.*\)\?"; then
    log "pulling Ollama model: $m"
    ollama pull "$m"
  else
    log "Ollama model present: $m"
  fi
done

# 3. DB init
if [[ -e "$DB_PATH" ]]; then
  log "DB exists, leaving as-is: $DB_PATH"
else
  log "initializing DB: $DB_PATH"
  "$VENV/bin/python" "$ROOT/scripts/init-memory.py" "$DB_PATH"
fi

# 4. Print MCP registration snippet (standalone-form helper; plugin form
#    skips this since .mcp.json is already declared in plugin.json)
if [[ -z "${CLAUDE_PLUGIN_DATA:-}" ]]; then
  cat <<EOF

==========================================================================
Setup complete for persona: $PERSONA  (standalone)
  DB:           $DB_PATH
  venv:         $VENV
  embed model:  $EMBED_MODEL
  judge model:  $JUDGE_MODEL

Register with your LLM client (example for Claude Code):
  Add to $ROOT/.mcp.json or ~/.claude.json:
    "command": "$VENV/bin/python"
    "args": ["-m", "server.main"]
    "cwd": "$ROOT"
    env: {
      "PERSONA_MEMORY_DB": "$DB_PATH",
      "OLLAMA_HOST": "http://localhost:11434",
      "PERSONA_EMBED_MODEL": "$EMBED_MODEL",
      "PERSONA_JUDGE_MODEL": "$JUDGE_MODEL"
    }

After registration, restart your LLM client and call health_check to verify.
==========================================================================
EOF
else
  log "plugin install — venv at $VENV will survive plugin version bumps"
fi
