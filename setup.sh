#!/usr/bin/env bash
# Set up a persona memory MCP server: venv + deps + Ollama models + DB.
#
# Usage:  ./setup.sh <persona-name>
# Example: ./setup.sh methodology-explorer
#
# After this completes, register the printed MCP server entry with your
# LLM client (Claude Code / codex CLI / Gemini CLI). See README.md.

set -euo pipefail

PERSONA="${1:-}"
if [[ -z "$PERSONA" ]]; then
  echo "Usage: $0 <persona-name>" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$ROOT/data"
DB_PATH="$DATA_DIR/$PERSONA.db"
VENV="$ROOT/.venv"
EMBED_MODEL="${PERSONA_EMBED_MODEL:-nomic-embed-text}"
JUDGE_MODEL="${PERSONA_JUDGE_MODEL:-gemma3:12b}"

log() { printf '\033[36m[setup]\033[0m %s\n' "$*"; }
err() { printf '\033[31m[setup]\033[0m %s\n' "$*" >&2; }

# 1. venv + deps
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
mkdir -p "$DATA_DIR"
if [[ -e "$DB_PATH" ]]; then
  log "DB exists, leaving as-is: $DB_PATH"
else
  log "initializing DB: $DB_PATH"
  "$VENV/bin/python" "$ROOT/scripts/init-memory.py" "$DB_PATH"
fi

# 4. Print MCP registration snippet
cat <<EOF

==========================================================================
Setup complete for persona: $PERSONA
  DB:           $DB_PATH
  embed model:  $EMBED_MODEL
  judge model:  $JUDGE_MODEL

Register with your LLM client:

  Claude Code (~/.claude.json or project .mcp.json):
    {
      "mcpServers": {
        "persona-memory-$PERSONA": {
          "command": "$VENV/bin/python",
          "args": ["-m", "server.main"],
          "cwd": "$ROOT",
          "env": {
            "PERSONA_MEMORY_DB": "$DB_PATH",
            "OLLAMA_HOST": "http://localhost:11434",
            "PERSONA_EMBED_MODEL": "$EMBED_MODEL",
            "PERSONA_JUDGE_MODEL": "$JUDGE_MODEL"
          }
        }
      }
    }

  codex CLI (~/.codex/config.toml):
    [mcp_servers.persona-memory-$PERSONA]
    command = "$VENV/bin/python"
    args = ["-m", "server.main"]
    cwd = "$ROOT"
    env = { PERSONA_MEMORY_DB = "$DB_PATH", OLLAMA_HOST = "http://localhost:11434" }

  Gemini CLI (~/.gemini/settings.json):
    {
      "mcpServers": {
        "persona-memory-$PERSONA": {
          "command": "$VENV/bin/python",
          "args": ["-m", "server.main"],
          "cwd": "$ROOT",
          "env": {
            "PERSONA_MEMORY_DB": "$DB_PATH",
            "OLLAMA_HOST": "http://localhost:11434"
          }
        }
      }
    }

After registration, restart your LLM client and call the health_check tool
to verify connectivity.
==========================================================================
EOF
