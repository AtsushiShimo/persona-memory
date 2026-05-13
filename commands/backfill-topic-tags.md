---
description: 過去 episode から topic_tags を遡及生成 (0.6.24 以前ログの救済). 重いので明示実行・限定実行対応.
allowed-tools: Bash
---

過去ログからトピックタグ (topic_tags) を遡って生成するコマンド。
0.6.24 以降の write LLM は turn 単位で tag を抽出するが、 それ以前に保存
された episode は通っていない。 このコマンドで過去ログを再投入してタグを救済し、
recall の topic_tag 近傍検索 (= 「Renju の話を再開しよう」 系の query) を
過去議論にも効かせる。

**facts は一切変更しない** ─ tag 抽出は別 prompt で動くため、 master が
懸念する write LLM の prompt 副作用は原理的に発生しない。

**重い処理**: light gemma3:4b で 1 episode あたり 1-3s。 数百件で 10-30 分。
`--limit` で部分実行可。 Ctrl+C で中断しても途中まで保存される。

## 引数なしで全件処理

このコマンドは引数なしで「topic_tags 未紐付の episode を全部処理」 する。
個別オプション (`--dry-run` / `--limit N` / `--since-episode-id N` /
`--count-only` / `--topic-id <fixed>`) を使いたい場合は直接 python module を呼ぶ:

```bash
PERSONA_MEMORY_DB=<persona.db path> \
  python -m scripts.topic.backfill_tags --db <persona.db path> --limit 50
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
  "$VENV_HOME/.venv/bin/python" -m scripts.topic.backfill_tags \
    --db "$DB" --count-only > /tmp/.pm_topic_backfill_count
PENDING=$(cat /tmp/.pm_topic_backfill_count | sed 's/[^0-9]//g')
rm -f /tmp/.pm_topic_backfill_count

echo "対象 episode: $PENDING 件 (推定 $((PENDING * 1 / 60))-$((PENDING * 3 / 60)) 分 @ light gemma3:4b)"
if [ "$PENDING" -eq 0 ]; then
  echo "処理対象がありません。"
  exit 0
fi

# 本実行
PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.topic.backfill_tags --db "$DB"
```
