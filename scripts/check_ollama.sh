#!/usr/bin/env bash
# Ollama reachability probe. Used by hook scripts to fail-loud when Ollama
# is down (so users notice their memory features have silently degraded).
#
# - exits 0 on reachable
# - exits 1 on unreachable, with a single-line warning to stderr
#
# Honors OLLAMA_HOST env var (default http://127.0.0.1:11434).

OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"

if curl -fsS --max-time 2 "$OLLAMA_HOST/api/tags" > /dev/null 2>&1; then
    exit 0
fi

echo "[persona-memory] Ollama unreachable at $OLLAMA_HOST — memory features degraded" >&2
exit 1
