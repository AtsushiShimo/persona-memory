#!/usr/bin/env bash
# Set up a persona memory MCP server: venv + deps + Ollama models + DB.
#
# Usage:  ./setup.sh <persona-name> [<project-dir>]
# Example: ./setup.sh code-reviewer ~/Desktop/myproject
#
# Layout (per project = per persona):
#   ~/.claude/plugins/data/<market>-<plugin>/.venv  ← shared venv (CLAUDE_PLUGIN_DATA)
#   <project-dir>/.persona-memory/<persona>.db      ← project-local memory
#   <project-dir>/.persona-memory/<persona>.config.env
#   <project-dir>/.persona-memory/active-persona
#
# Plugin form: invoked via /persona-memory:init. CLAUDE_PLUGIN_DATA holds
# the shared venv; project dir comes from arg 2 (or the slash command's pwd).
# Standalone form: invoked from a cloned repo without CLAUDE_PLUGIN_DATA.
# Then both venv and DB go under $ROOT/.

set -euo pipefail

PERSONA="${1:-}"
PROJECT_DIR="${2:-}"
if [[ -z "$PERSONA" ]]; then
  echo "Usage: $0 <persona-name> [<project-dir>]" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Decide where the shared venv lives.
if [[ -n "${CLAUDE_PLUGIN_DATA:-}" ]]; then
  VENV_HOME="$CLAUDE_PLUGIN_DATA"
else
  VENV_HOME="$ROOT"
fi
mkdir -p "$VENV_HOME"

# Decide where project-local persona data lives.
if [[ -z "$PROJECT_DIR" ]]; then
  PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
fi
PERSONA_DIR="$PROJECT_DIR/.persona-memory"
mkdir -p "$PERSONA_DIR"

DB_PATH="$PERSONA_DIR/$PERSONA.db"
VENV="$VENV_HOME/.venv"
EMBED_MODEL="${PERSONA_EMBED_MODEL:-nomic-embed-text}"
LIGHT_MODEL="${PERSONA_LIGHT_MODEL:-${PERSONA_JUDGE_MODEL:-gemma3:4b}}"
HEAVY_MODEL="${PERSONA_HEAVY_MODEL:-gemma3:12b}"

log() { printf '\033[36m[setup]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[setup]\033[0m %s\n' "$*" >&2; }

# 1. venv + deps (in $DATA_DIR/.venv so it persists across plugin updates)
if [[ ! -d "$VENV" ]]; then
  log "creating venv at $VENV"
  python3 -m venv "$VENV"
fi
log "installing Python deps"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet sqlite-vec httpx

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
for m in "$EMBED_MODEL" "$LIGHT_MODEL" "$HEAVY_MODEL"; do
  if ! ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$m\(:.*\)\?"; then
    log "pulling Ollama model: $m"
    ollama pull "$m"
  else
    log "Ollama model present: $m"
  fi
done

# 3. DB init (project-local) — 新スキーマ (scripts.db.migrate)
if [[ -e "$DB_PATH" ]]; then
  log "DB exists, leaving as-is: $DB_PATH"
else
  log "initializing DB: $DB_PATH"
  (cd "$ROOT" && PYTHONPATH="$ROOT" "$VENV/bin/python" -m scripts.db.migrate "$DB_PATH")
fi

log "venv:         $VENV  (shared across all projects)"
log "persona DB:   $DB_PATH  (project-local)"
log "models:       light=$LIGHT_MODEL  heavy=$HEAVY_MODEL  embed=$EMBED_MODEL"
