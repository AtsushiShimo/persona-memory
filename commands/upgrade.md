---
description: プラグイン更新後の non-destructive アップグレード (boot 層 default を最新に refresh、ペルソナ・記憶は無傷)
allowed-tools: Bash
---

プラグインを新バージョンに更新した後に走らせるコマンド。
**ペルソナ固有設定 (役割・名前・性格・口調 等) と episodes / 他カテゴリの
記憶は一切触らない**。共通の default 行動指針 (response_brevity /
confirmation_before_acting / silent_memory / forbid_auto_memory 等) だけを
最新に保つ。

`/persona-memory:reset` → `/persona-memory:init` のような破壊的やり直しは
**不要**。これを 1 回叩けばプラグイン更新が反映される。

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

# 1. boot 層 default を idempotent に refresh
echo "=== boot 層 default を refresh ==="
PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" "$PLUGIN_ROOT/scripts/upgrade.py" --db "$DB"

# 2. settings.local.json の env を最新化 (CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX)
#    値はペルソナ名そのもの (区切り文字 ': ' は付けない、Claude Code 側で
#    `<prefix>-<adj>-<noun>` 形式に組まれる)
mkdir -p "$PROJECT_DIR/.claude"
SETTINGS_FILE="$PROJECT_DIR/.claude/settings.local.json"
"$VENV_HOME/.venv/bin/python" - "$SETTINGS_FILE" "$ACTIVE" <<'PY'
import json, pathlib, sys
fp = pathlib.Path(sys.argv[1])
prefix = sys.argv[2]
data = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
env = data.get("env") or {}
if env.get("CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX") == prefix:
    print(f"[ok] env CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX 既に最新 ({prefix})")
else:
    env["CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX"] = prefix
    data["env"] = env
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ok] {fp} に CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX={prefix} を設定")
PY

echo
echo "=== upgrade 完了: $ACTIVE ==="
echo "ペルソナ・記憶は無傷で、共通行動指針だけ最新になりました。"
echo "Claude Code を完全終了 → 再起動で反映されます。"
```

## 完了後ユーザーへ

- 何件 inserted / updated / unchanged だったかを 1-2 行で報告
- ペルソナ固有設定や episodes は無傷であることを明示
- Claude Code の再起動を促す
