---
description: 反省モードを緊急停止 (現在の反省状態を clear + 怒気検知を一時無効化). セッション維持したまま即時反映.
allowed-tools: Bash
---

反省モードの緊急 revert 経路。怒気検知が誤発火し続ける / LLM 判定がハング
する / instruction が壊れて main agent が動けない、 等の状況で叩く。
DB persisted flag を切替えるのでセッション維持したまま即時反映。

復帰するには `/persona-memory:reflection-on` を叩く。

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

if [ -z "$ACTIVE" ]; then
  echo "アクティブなペルソナがありません。/persona-memory:init で先に作成してください。"
  exit 1
fi

DB="$DATA_DIR/$ACTIVE.cozo.db"
[ -f "$DB" ] || DB="$DATA_DIR/$ACTIVE.db"

PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.reflection.emergency off
```

## 完了後ユーザーへ

- 成功時 (`ok=true`): 「反省モードを停止しました」 を 1 行で報告。
  `reflection_state_cleared=true` だった時は 「進行中の反省状態も解除しました」
  を添える。
- 内部用語は使わず、 「思い出す力」「気持ちの整理」 等の自然語で言い換える。
- 復帰したい時は `/persona-memory:reflection-on` を案内。
