---
description: 過去叱責 (HANDOFF Section 14.5) を lesson + trigger として記憶に刻む (0.7.7 反省モード基盤). 冪等.
allowed-tools: Bash
---

このコマンドは、 過去にマスターから叱責された 11 項のパターン (HANDOFF.md
Section 14.5) を「再発防止 lesson」 として記憶に書き込む。 反省モード本体
(怒気検知 → 謝罪 → 改善ルール提示) が動き出す前の **初期 seed** として位置付ける。

書き込まれる lesson:
- 11 項の lesson fact (category='lesson', importance=10)
- 各 lesson に紐付く想起トリガー (path_edit / path_read / bash_cmd /
  prompt_intent / general). 該当操作時に PreToolUse hook が `block` or
  `warn` を発火する.

**冪等**: 何度実行しても結果は同じ. 既存 lesson があれば value 更新, trigger は
一掃してから再登録される.

**前提**: Cozo DB が初期化済であること (`/persona-memory:init` 実行済).

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
  echo "アクティブなペルソナがありません。/persona-memory:init で先に作成してください。"
  exit 1
fi

DB="$DATA_DIR/$ACTIVE.cozo.db"

PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.reflection.seed_past_lessons
```

## 完了後ユーザーへ

- 書き込まれた lesson 件数と trigger 件数を 1 行で報告
- 「これ以降、 過去の叱責パターンに該当する操作・発話で `block` / `warn` が
  発火するようになりました」 と添える
- 詳細な lesson 一覧は `/persona-memory:list` で確認可能
