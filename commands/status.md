---
description: Show current persona state, DB stats, and Ollama health
allowed-tools: Bash
---

現在のペルソナ状態とシステムヘルスを表示します。

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
DATA_DIR="$PROJECT_DIR/.persona-memory"
SCRIPT_HOME="$PLUGIN_ROOT" CLAUDE_PROJECT_DIR="$PROJECT_DIR" \
  . "$PLUGIN_ROOT/scripts/load_persona_env.sh"

echo "=== persona-memory status ==="
echo "plugin root:  $PLUGIN_ROOT"
echo "project dir:  $PROJECT_DIR"
echo "persona dir:  $DATA_DIR"
echo
echo "active persona:  $(cat "$DATA_DIR/active-persona" 2>/dev/null || echo '(none)')"
echo "DB:              $PERSONA_MEMORY_DB"
echo "light model:     $PERSONA_LIGHT_MODEL"
echo "heavy model:     $PERSONA_HEAVY_MODEL"
echo "embed model:     $PERSONA_EMBED_MODEL"
echo "Ollama host:     $OLLAMA_HOST"

echo
echo "--- DB stats ---"
if [ -f "$PERSONA_MEMORY_DB" ]; then
  sqlite3 "$PERSONA_MEMORY_DB" <<'SQL'
.headers on
.mode column
SELECT category, COUNT(*) as count FROM facts WHERE status='active' GROUP BY category ORDER BY count DESC;
SELECT '--- episodes ---' as info;
SELECT role, COUNT(*) as count FROM episodes GROUP BY role ORDER BY count DESC;
SQL
else
  echo "(DB does not exist yet — run /persona-memory:init)"
fi

echo
echo "--- Ollama health ---"
if curl -sS --max-time 2 "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
  echo "Ollama daemon: OK ($OLLAMA_HOST)"
  for m in "$PERSONA_LIGHT_MODEL" "$PERSONA_HEAVY_MODEL" "$PERSONA_EMBED_MODEL"; do
    if curl -sS "$OLLAMA_HOST/api/tags" 2>/dev/null | grep -q "\"name\":\"$m"; then
      echo "  ✓ $m"
    else
      echo "  ✗ $m  (not pulled — run: ollama pull $m)"
    fi
  done
else
  echo "Ollama daemon: NOT REACHABLE at $OLLAMA_HOST"
  echo "  start with: ollama serve"
fi
```

実行結果を整形してユーザーに伝えてください。
