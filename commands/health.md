---
description: ペルソナの体調 (DB / 設定 / Ollama / 直近 ingest 健全性) を包括チェックして JSON で返す
allowed-tools: Bash
---

ペルソナのストレージとローカル LLM の健康診断。`「体調はどうだい?」` 系の
発話でも自動的に呼ばれる (boot 層 `persona/health_check_trigger`)。

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
  echo "アクティブなペルソナがありません。/persona-memory:init で先に作成してください。"
  exit 1
fi

DB="$DATA_DIR/$ACTIVE.db"

PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.health
```

## 完了後ユーザーへ

- `ok=true` なら 1 行「元気です」 系で要約 + facts_active の件数を伝える
- `warnings` があれば内容を箇条書きで伝え、必要なら `/persona-memory:upgrade` を提案
- `errors` があれば率直に伝え、原因 (Ollama 落ち / model 未取得 / DB 異常) と対処を提案
- 内部用語 (DB / Ollama / embedding 等) は使わず、自然語で言い換えて伝える
  (例: 「Ollama 到達不可」 → 「思い出す力が落ちています」)
