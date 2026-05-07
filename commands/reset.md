---
description: アクティブなペルソナを完全に削除します (DB / config / active-persona すべて削除)
allowed-tools: Bash, AskUserQuestion
---

アクティブなペルソナを完全に削除します。**復元不能**なので慎重に。

## 1. 現状を確認

```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
DATA_DIR="$PROJECT_DIR/.persona-memory"

if [ ! -d "$DATA_DIR" ]; then
  echo "削除対象なし: $DATA_DIR が存在しません"
  exit 0
fi

ACTIVE=""
[ -r "$DATA_DIR/active-persona" ] && ACTIVE="$(cat "$DATA_DIR/active-persona")"

echo "===== persona-memory 状態 ====="
echo "data dir: $DATA_DIR"
echo "active:   ${ACTIVE:-(none)}"
echo
echo "削除候補:"
ls -la "$DATA_DIR" 2>/dev/null
echo
if [ -n "$ACTIVE" ] && [ -f "$DATA_DIR/$ACTIVE.db" ]; then
  facts=$(sqlite3 "$DATA_DIR/$ACTIVE.db" \
    "SELECT COUNT(*) FROM facts WHERE status='active'" 2>/dev/null)
  eps=$(sqlite3 "$DATA_DIR/$ACTIVE.db" \
    "SELECT COUNT(*) FROM episodes" 2>/dev/null)
  echo "[$ACTIVE] active facts=$facts, episodes=$eps"
fi
```

## 2. AskUserQuestion で確認

- **header**: `削除確認`
- **question**: "<上で表示した数を含めて> 本当に削除しますか?"
- **multiSelect**: false
- **options**:
  1. label: `アクティブのみ削除` / description: "<アクティブ名> の DB/config/active-persona を削除。他のペルソナは残す"
  2. label: `すべて削除 (.persona-memory/ ごと)` / description: "このプロジェクトのすべてのペルソナ DB を消す"
  3. label: `キャンセル` / description: "何もしない"

## 3. 実行

選択に応じて Bash を実行:

### 「アクティブのみ削除」 を選んだ場合

```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
DATA_DIR="$PROJECT_DIR/.persona-memory"
ACTIVE="$(cat "$DATA_DIR/active-persona" 2>/dev/null)"

if [ -z "$ACTIVE" ]; then
  echo "active persona が無いので何もしません"
  exit 0
fi

rm -f "$DATA_DIR/$ACTIVE.db" "$DATA_DIR/$ACTIVE.config.env"
rm -f "$DATA_DIR/active-persona"
rm -f "$DATA_DIR/debug-recall.log"
echo "[ok] 削除完了: $ACTIVE"
echo
echo "次のステップ: /persona-memory:init で新規セットアップ"
```

### 「すべて削除」 を選んだ場合

```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
DATA_DIR="$PROJECT_DIR/.persona-memory"

rm -rf "$DATA_DIR"
echo "[ok] $DATA_DIR を削除しました"
echo
echo "次のステップ: /persona-memory:init で新規セットアップ"
```

### 「キャンセル」 を選んだ場合

何もしない。"キャンセルしました。" とだけ返す。

## 完了後

ユーザーに削除サマリを 2-3 行で報告し、`/persona-memory:init` でセットアップし直すよう案内する。
