---
description: 過去 episode から discussion_nodes を遡及生成 (0.6.17 以前ログの救済). 重いので明示実行・限定実行対応.
allowed-tools: Bash
---

過去ログから議論ノード (discussion_nodes) を遡って生成するコマンド。
0.6.17 以降の write LLM は議論ノード抽出を含むが、 それ以前に保存された
episode は通っていない。 このコマンドで過去ログを再投入してノードを救済する。

**facts は一切変更しない** ─ master が懸念する write LLM の prompt 副作用は
原理的に発生しない。

**重い処理**: 1 episode あたり LLM 15-30s。 全部処理すると数十分かかる場合あり。
`--limit` で部分実行可。 Ctrl+C で中断しても途中まで保存される (再実行で続行可)。

## 引数なしで全件処理

このコマンドは引数なしで「discussion_nodes 未生成の episode を全部処理」 する。
個別オプション (`--dry-run` / `--limit N` / `--since-episode-id N` / `--count-only`)
を使いたい場合は直接 python module を呼ぶ:

```bash
PERSONA_MEMORY_DB=<persona.db path> \
  python -m scripts.discussion.backfill --db <persona.db path> --limit 20
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
  echo "アクティブなペルソナがありません。/persona-memory:init で先に作成してください。"
  exit 1
fi

DB="$DATA_DIR/$ACTIVE.db"

# まず件数と推定時間だけ見せる (LLM 呼ばない軽量 query)
PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.discussion.backfill \
    --db "$DB" --count-only > /tmp/.pm_backfill_count
PENDING=$(cat /tmp/.pm_backfill_count)
rm -f /tmp/.pm_backfill_count

echo "対象 episode: $PENDING 件 (推定 $((PENDING * 15 / 60))-$((PENDING * 30 / 60)) 分)"
if [ "$PENDING" -eq 0 ]; then
  echo "処理対象がありません。"
  exit 0
fi

# 本実行
PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.discussion.backfill --db "$DB"
```

## 完了後ユーザーへ

- 何 episode 処理して何 node 追加できたか 1-2 行で報告
- 中断された (`interrupted=1`) 場合はその旨を伝え、 残件を再実行する案内
- 既存 facts は無変更である旨を明示
