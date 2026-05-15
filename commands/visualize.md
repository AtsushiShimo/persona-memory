---
description: 議論グラフを 3D 可視化 (on-demand). Cozo の discussion_node + discussion_edge を self-contained HTML に dump してブラウザで開く.
allowed-tools: Bash
---

議論グラフ (賛同 / 反論 / 派生 / 決定 / 撤回 等) を 3D force-directed graph
として可視化する。 ローカル HTML を生成し、 ブラウザで自動オープンする。

**on-demand 実行のみ** — hook 経路では呼ばない。

## 引数なしで全件可視化

このコマンドは引数なしで全 discussion_node + edge を可視化する。
個別オプションは直接 python module を呼ぶ:

```bash
PERSONA_MEMORY_DB=<persona.cozo.db> \
  python -m scripts.db_cozo.visualize \
    --db <persona.cozo.db> \
    --topic <topic_id>      # 特定 topic のみ
    --days 7                # 過去 7 日のみ
    --limit 200             # 最新 200 node のみ (混雑回避)
    --json-only             # JSON を stdout に
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

if [ -z "$ACTIVE" ] || [ ! -f "$DATA_DIR/$ACTIVE.cozo.db" ]; then
  echo "Cozo DB が見つかりません。 /persona-memory:upgrade-cozo を先に実行してください。"
  exit 1
fi

DB="$DATA_DIR/$ACTIVE.cozo.db"

PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.db_cozo.visualize \
    --db "$DB" \
    --out-dir "$DATA_DIR/visualize" \
    > /tmp/.pm_visualize_out
RC=$?
cat /tmp/.pm_visualize_out
HTML=$(grep '^  generated: ' /tmp/.pm_visualize_out | sed 's|^  generated: ||')
rm -f /tmp/.pm_visualize_out

if [ "$RC" -eq 0 ] && [ -n "$HTML" ] && [ -f "$HTML" ]; then
  open "$HTML" 2>/dev/null || echo "ブラウザで開いてください: $HTML"
fi
```

## 完了後ユーザーへ

- 何 node / edge / topic を可視化したか 1 行で報告
- 生成 HTML の path を案内 (自動 open 済)
- 混雑時は `--limit 100` や `--topic <id>` で絞る案内
