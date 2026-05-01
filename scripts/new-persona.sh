#!/usr/bin/env bash
# Clone this persona-memory project into a fresh directory configured for a
# new persona. Each persona owns its own directory, CLAUDE.md, .mcp.json,
# and DB — matching the design "1 親エージェント = 1 プロジェクト = 1 記憶
# = 1 個性 (ペルソナ)".
#
# Usage:   scripts/new-persona.sh <persona-name> <target-dir>
# Example: scripts/new-persona.sh satoshi ~/Desktop/claude_dev/satoshi
#
# After this script finishes:
#   cd <target-dir> && claude
set -euo pipefail

PERSONA="${1:-}"
TARGET="${2:-}"

if [[ -z "$PERSONA" || -z "$TARGET" ]]; then
  echo "Usage: $0 <persona-name> <target-dir>" >&2
  echo "Example: $0 satoshi ~/Desktop/claude_dev/satoshi" >&2
  exit 1
fi

if [[ -e "$TARGET" ]]; then
  echo "Target already exists: $TARGET" >&2
  exit 1
fi

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required but not found in PATH" >&2
  exit 2
fi

log() { printf '\033[36m[new-persona]\033[0m %s\n' "$*"; }

log "cloning $SOURCE -> $TARGET (persona: $PERSONA)"
mkdir -p "$TARGET"
TARGET_ABS="$(cd "$TARGET" && pwd)"

# Copy template files; skip volatile, per-persona, and host-specific artifacts.
rsync -a \
  --exclude='.git/' \
  --exclude='data/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  "$SOURCE/" "$TARGET_ABS/"

# Rewrite .mcp.json so paths point at the new target dir + per-persona DB.
python3 - "$TARGET_ABS" "$PERSONA" <<'PY'
import json, sys
target, persona = sys.argv[1], sys.argv[2]
path = f"{target}/.mcp.json"
with open(path) as f:
    cfg = json.load(f)
srv = cfg["mcpServers"]["persona-memory"]
srv["command"] = f"{target}/.venv/bin/python"
srv["cwd"] = target
srv["env"]["PERSONA_MEMORY_DB"] = f"{target}/data/{persona}.db"
with open(path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
PY
log "rewrote .mcp.json -> data/$PERSONA.db"

# Tag CLAUDE.md with the persona name in the title.
python3 - "$TARGET_ABS" "$PERSONA" <<'PY'
import re, pathlib, sys
target, persona = sys.argv[1], sys.argv[2]
p = pathlib.Path(target) / "CLAUDE.md"
content = p.read_text()
content = re.sub(
    r"^# persona-memory.*\n",
    f"# persona-memory: {persona}\n",
    content,
    count=1,
)
p.write_text(content)
PY
log "tagged CLAUDE.md title with persona name"

# Bootstrap venv + Ollama models + DB.
cd "$TARGET_ABS"
./setup.sh "$PERSONA"

cat <<EOF

==========================================================================
Persona created: $PERSONA
  Directory:  $TARGET_ABS
  DB:         $TARGET_ABS/data/$PERSONA.db
  MCP config: $TARGET_ABS/.mcp.json (already wired to this dir)

Next:
  cd "$TARGET_ABS"
  claude

The first session starts with empty persona facts. Talk to the agent and
give it personality / behavior instructions — those get auto-saved as
category="persona" and re-injected on every future SessionStart.
==========================================================================
EOF
