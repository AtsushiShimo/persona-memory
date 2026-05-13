---
description: SQLite → Cozo 移行 + 議論グラフ backfill (新「流れ再構築」 機能を有効化). 重い処理.
allowed-tools: Bash
---

`<persona>.db` (SQLite) を `<persona>.cozo.db` (Cozo embedded) に移行し、
過去 episode から discussion_node + discussion_edge を遡及生成する.

完了後、 fresh session で発話すると recall に「## 関連する議論 — 流れ」
ブロックが追加表示される. SQLite 経路は無傷で並走するため fallback 可.

**重い処理** — backfill が 1 episode あたり LLM 5-15s. 数百 episodes で
30 分〜数時間. `--limit` で部分実行する場合は直接 module を呼ぶ:

```bash
python -m scripts.db_cozo.backfill_graph --db <persona>.cozo.db --limit 100
```

## 実行

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$PLUGIN_ROOT" ] || [ ! -d "$PLUGIN_ROOT" ]; then
  PLUGIN_ROOT=$(ls -d "$HOME"/.claude/plugins/cache/persona-memory/persona-memory/*/ 2>/dev/null \
                | sort -V | tail -1 | sed 's|/$||')
fi
if [ -z "$PLUGIN_ROOT" ] || [ ! -d "$PLUGIN_ROOT" ]; then
  echo "ERROR: persona-memory plugin cache not found." >&2
  exit 1
fi

case "$PLUGIN_ROOT" in
  */plugins/cache/*/*/*)
    _plugin_dir="$(dirname "$PLUGIN_ROOT")"
    _market_dir="$(dirname "$_plugin_dir")"
    _plugins_root="$(dirname "$(dirname "$_market_dir")")"
    VENV_HOME="$_plugins_root/data/$(basename "$_market_dir")-$(basename "$_plugin_dir")"
    ;;
  *) VENV_HOME="$PLUGIN_ROOT" ;;
esac

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
DATA_DIR="$PROJECT_DIR/.persona-memory"
ACTIVE=""
[ -r "$DATA_DIR/active-persona" ] && ACTIVE="$(cat "$DATA_DIR/active-persona")"

if [ -z "$ACTIVE" ] || [ ! -f "$DATA_DIR/$ACTIVE.db" ]; then
  echo "アクティブなペルソナがありません. /persona-memory:init で先に作成してください." >&2
  exit 1
fi

SRC_DB="$DATA_DIR/$ACTIVE.db"
DST_DB="$DATA_DIR/$ACTIVE.cozo.db"

echo "=== Step 1/2: SQLite → Cozo 移行 (物理 backup 自動作成) ==="
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.db_cozo.migrate_from_sqlite \
    --src "$SRC_DB" --dst "$DST_DB"

echo
echo "=== Step 2/2: 議論グラフ backfill (重い: 1 episode あたり 5-15s) ==="
PERSONA_MEMORY_DB="$DST_DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.db_cozo.backfill_graph --db "$DST_DB"

echo
echo "完了. fresh session で発話すると 「## 関連する議論」 ブロックが additionalContext に追加されます."
echo "効果が見えない場合は PERSONA_COZO_DISABLE 環境変数が立っていないか確認してください."
```
