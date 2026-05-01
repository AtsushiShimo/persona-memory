---
description: Change Ollama models used by the active persona (light = write-side, heavy = read-side compression)
allowed-tools: Bash, Read, Write, AskUserQuestion
---

アクティブペルソナの Ollama モデル設定を変更します。

## 現在の設定を表示

まず現在の active persona と使用モデルを確認:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
DATA_DIR="${CLAUDE_PLUGIN_DATA:-$PLUGIN_ROOT/data}"
ACTIVE=$(cat "$DATA_DIR/active-persona" 2>/dev/null)
CONFIG="$DATA_DIR/$ACTIVE.config.env"

if [ -z "$ACTIVE" ] || [ ! -f "$CONFIG" ]; then
  echo "アクティブなペルソナがありません。/persona-memory:init で先に作成してください。"
  exit 1
fi

echo "現在のペルソナ: $ACTIVE"
echo "config.env: $CONFIG"
echo
cat "$CONFIG"
```

ユーザーに整形して提示してください。

## 変更する項目を聞く

AskUserQuestion で:

- **質問**: "どのモデルを変更しますか?"
- **選択肢**:
  - `軽量モデル (light) のみ` — 書き込み側 (Stop / PreCompact / SessionEnd)
  - `重量モデル (heavy) のみ` — 読み出し圧縮側 (proxy_recall)
  - `両方` — light と heavy の両方
  - `何もしない / キャンセル`
- multiSelect: false

## 軽量モデル (light) の選択肢

選んだ場合のみ AskUserQuestion で:

- **質問**: "軽量モデル (書き込み時に毎ターン発火するため応答遅延を抑えたい) を選んでください"
- **選択肢** (各選択肢に header と description):
  - `gemma3:4b` / "推奨・~3.3GB / バランス良好"
  - `gemma3:1b` / "超軽量・~815MB / 最速だが粗い"
  - `qwen2.5:3b-instruct` / "~1.9GB / 日本語強"
  - `llama3.2:3b-instruct` / "~2.0GB"
  - `gemma3:12b` / "重量と同じにする (品質優先派)"
  - `その他` / "自由入力で別モデル名を指定"

## 重量モデル (heavy) の選択肢

選んだ場合のみ AskUserQuestion で:

- **質問**: "重量モデル (読み出し圧縮で context に直接注入される最終生成物。品質重視) を選んでください"
- **選択肢**:
  - `gemma3:12b` / "推奨・~7GB / 品質高"
  - `gemma3:27b` / "大型・~16GB / 最高品質・要メモリ"
  - `qwen2.5:14b-instruct` / "~8.5GB / 日本語強"
  - `llama3.1:8b-instruct` / "~4.7GB"
  - `gemma3:4b` / "light と同じにする (軽量機派)"
  - `その他` / "自由入力"

## 適用

変更すべきモデルが決まったら:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(pwd)}"
DATA_DIR="${CLAUDE_PLUGIN_DATA:-$PLUGIN_ROOT/data}"
ACTIVE=$(cat "$DATA_DIR/active-persona")
CONFIG="$DATA_DIR/$ACTIVE.config.env"

NEW_LIGHT="<選択された light モデル、変更しないなら空>"
NEW_HEAVY="<選択された heavy モデル、変更しないなら空>"

# 必要なら Ollama pull (まだ未取得のモデルなら時間がかかる)
for m in "$NEW_LIGHT" "$NEW_HEAVY"; do
  [ -z "$m" ] && continue
  if ! ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$m\(:.*\)\?"; then
    echo "Ollama: $m を pull します..."
    ollama pull "$m"
  fi
done

# config.env を書き換え (sed で in-place)
if [ -n "$NEW_LIGHT" ]; then
  sed -i.bak -E "s|^PERSONA_LIGHT_MODEL=.*$|PERSONA_LIGHT_MODEL=\"$NEW_LIGHT\"|" "$CONFIG"
fi
if [ -n "$NEW_HEAVY" ]; then
  sed -i.bak -E "s|^PERSONA_HEAVY_MODEL=.*$|PERSONA_HEAVY_MODEL=\"$NEW_HEAVY\"|" "$CONFIG"
fi
rm -f "${CONFIG}.bak"

echo "更新後の config.env:"
cat "$CONFIG"
```

完了したらユーザーに:
- 変更前 / 変更後の比較
- **MCP サーバーには即時反映されない** (env は MCP 起動時に評価されるため、Claude Code を完全終了→再起動が必要)
- hooks (Stop / UserPromptSubmit など) は次回発火時に config.env を再 source するので即時反映される
