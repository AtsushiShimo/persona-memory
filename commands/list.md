---
description: List all personas registered in this persona-memory install
allowed-tools: Bash
---

登録済みペルソナ一覧を表示します。

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
DATA_DIR="${CLAUDE_PLUGIN_DATA:-$PLUGIN_ROOT/data}"
ACTIVE=""
[ -r "$DATA_DIR/active-persona" ] && ACTIVE="$(cat "$DATA_DIR/active-persona" 2>/dev/null)"

echo "active: ${ACTIVE:-(none)}"
echo "data dir: $DATA_DIR"
echo
echo "personas:"
# Look for *.config.env (per-persona config files) and *.db (raw DBs without config)
shopt -s nullglob 2>/dev/null || true
for cfg in "$DATA_DIR"/*.config.env; do
  name="$(basename "$cfg" .config.env)"
  db="$DATA_DIR/$name.db"
  marker=" "
  [ "$name" = "$ACTIVE" ] && marker="*"
  if [ -f "$db" ]; then
    facts=$(sqlite3 "$db" "SELECT COUNT(*) FROM facts WHERE status='active'" 2>/dev/null || echo "?")
    eps=$(sqlite3 "$db" "SELECT COUNT(*) FROM episodes" 2>/dev/null || echo "?")
    printf "  %s %-20s  facts=%s episodes=%s\n" "$marker" "$name" "$facts" "$eps"
  else
    printf "  %s %-20s  (no DB)\n" "$marker" "$name"
  fi
done

# Also pick up DBs without a config.env (legacy installs)
for db in "$DATA_DIR"/*.db; do
  name="$(basename "$db" .db)"
  cfg="$DATA_DIR/$name.config.env"
  if [ ! -f "$cfg" ]; then
    facts=$(sqlite3 "$db" "SELECT COUNT(*) FROM facts WHERE status='active'" 2>/dev/null || echo "?")
    eps=$(sqlite3 "$db" "SELECT COUNT(*) FROM episodes" 2>/dev/null || echo "?")
    marker=" "
    [ "$name" = "$ACTIVE" ] && marker="*"
    printf "  %s %-20s  facts=%s episodes=%s  (legacy: no config.env)\n" "$marker" "$name" "$facts" "$eps"
  fi
done
```

実行結果をユーザーに分かりやすく整形して伝えてください。`*` 印がアクティブペルソナです。
