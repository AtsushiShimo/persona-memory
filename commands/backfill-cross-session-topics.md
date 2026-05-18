---
description: 旧 episode の topic_id を主題ベース cluster に振り直す (Cross-Session Topic Merge backfill). session_id を topic 代用していた legacy データを救済.
allowed-tools: Bash
---

「session 跨ぎで同議題なら同 topic に集約」 する Cross-Session Topic Merge の
**遡及 backfill**。

対象は **`topic_id == session_id` の episode のみ** (= legacy fallback で
session_id を topic 代わりに置いていた古いデータ)。 sub- prefix や明示割り当て
済の topic_id は触らない (safety net)。

**facts は一切変更しない** ─ topic_id 列だけを書き換える。

**重い処理**: 1 episode あたり LLM 2-5s (light モデル使用)。 全部処理すると
数分〜数十分。 `--limit` で部分実行可。 Ctrl+C で中断しても途中まで反映済
(再実行で続行可)。

## 引数なしで全件処理

```bash
PERSONA_MEMORY_DB=<persona.cozo.db path> \
  python -m scripts.db_cozo.backfill_cross_session_topics \
    --db <persona.cozo.db path>
```

オプション:
- `--dry-run`: 判定だけして書き込まない
- `--limit N`: 先頭 N 件で打ち切り
- `--count-only`: 対象件数だけ stdout に出して終了

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
  echo "Cozo DB が見つかりません。先に /persona-memory:init でペルソナを作成してください。"
  exit 1
fi

DB="$DATA_DIR/$ACTIVE.cozo.db"

# 件数と推定時間を先に出す
PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.db_cozo.backfill_cross_session_topics \
    --db "$DB" --count-only > /tmp/.pm_cstopic_count
PENDING=$(cat /tmp/.pm_cstopic_count)
rm -f /tmp/.pm_cstopic_count

echo "対象 episode: $PENDING 件 (推定 $((PENDING * 2 / 60))-$((PENDING * 5 / 60)) 分)"
if [ "$PENDING" -eq 0 ]; then
  echo "処理対象がありません。"
  exit 0
fi

PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" -m scripts.db_cozo.backfill_cross_session_topics \
    --db "$DB"
```

## 完了後ユーザーへ

- 何 episode 処理して何件 merge されたか 1-2 行で報告
- 中断された (`interrupted=1`) 場合はその旨を伝え、 残件の再実行を案内
- facts は無変更である旨を明示
- 続けて `/persona-memory:backfill-discussion` を再実行すると、 振り直し後の
  topic_id を前提に discussion_edge も追加で張れる可能性がある旨を案内
