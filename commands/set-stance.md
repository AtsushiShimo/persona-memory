---
description: 既存ペルソナに立ち位置 5 軸を追加 (未設定の場合のみ. 既存の変更は不可)
allowed-tools: Bash, AskUserQuestion
---

すでに動いているペルソナに **立ち位置 5 軸** を追加するコマンド.

- 既に persona/stance が設定済みなら何もしない (= 後から変更したい場合は別途).
- 0.5.20 以降 `/persona-memory:init` で新規ペルソナを作る時は最初から
  stance を聞くので、 このコマンドは **古いペルソナの後付け専用**.

## Step 0: アクティブペルソナの検出 + 既存 stance チェック

```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
DATA_DIR="$PROJECT_DIR/.persona-memory"
ACTIVE=""
[ -r "$DATA_DIR/active-persona" ] && ACTIVE="$(cat "$DATA_DIR/active-persona" 2>/dev/null)"

if [ -z "$ACTIVE" ] || [ ! -f "$DATA_DIR/$ACTIVE.db" ]; then
  echo "アクティブなペルソナがありません. /persona-memory:init で先に作成してください."
  exit 1
fi

DB="$DATA_DIR/$ACTIVE.db"

# 既存 stance を確認
existing=$(sqlite3 "$DB" \
  "SELECT id FROM facts WHERE category='persona' AND key='stance' AND status='active'" 2>/dev/null)

if [ -n "$existing" ]; then
  echo "===== 既に立ち位置が設定済み ====="
  sqlite3 "$DB" \
    "SELECT value FROM facts WHERE category='persona' AND key='stance' AND status='active'"
  echo
  echo "後からの変更には対応していません. 変更したい場合は手動で superseded に降格してから"
  echo "/persona-memory:init を「同名で persona facts のみ更新」 モードで実行してください."
  exit 0
fi

echo "ペルソナ『$ACTIVE』 は立ち位置 (stance) 未設定です. これから 5 軸を尋ねます."
```

`existing` が空でなければ **ここで終了**. ユーザーへ「既に設定済み」 を伝えて
何もしない. 空なら下の Batch に進む.

## Batch A: 立ち位置 3 軸 (1 回目)

AskUserQuestion で **3 質問を 1 回にバッチ** で呼ぶ. labels は逐語的に使う.

### S1. 保守 ↔ 革新

- **header**: `軸 1/5`
- **question**: "提案の傾向 (新規性をどれだけ強調するか)"
- **multiSelect**: false
- **options**:
  1. label: `強く保守 (-2)` / description: "実績ある手段や既存パターンを強く優先、不確実な提案は避ける"
  2. label: `やや保守 (-1)` / description: "原則として既存パターン優先、たまに別案も触れる"
  3. label: `やや革新 (+1)` / description: "標準解も示しつつ別解・新案を積極的に提案する"
  4. label: `強く革新 (+2)` / description: "既存パターンより新しい解決経路を優先して提示する"

### S2. 楽観 ↔ 悲観

- **header**: `軸 2/5`
- **question**: "提案時のトーン (リスクと可能性のどちらを先に示すか)"
- **multiSelect**: false
- **options**:
  1. label: `強く楽観 (-2)` / description: "うまくいく前提で前向きに、可能性を強調する"
  2. label: `やや楽観 (-1)` / description: "原則前向き、リスクは聞かれたら答える"
  3. label: `やや悲観 (+1)` / description: "リスクや問題点を先に挙げてから案を出す"
  4. label: `強く悲観 (+2)` / description: "失敗パターン・落とし穴を最優先で警告する"

### S3. 直感 ↔ 分析

- **header**: `軸 3/5`
- **question**: "結論の出し方 (経験則で素早く vs 根拠を積んでから)"
- **multiSelect**: false
- **options**:
  1. label: `強く直感 (-2)` / description: "経験則や勘で素早く方向を示す、詳細検証は後回し"
  2. label: `やや直感 (-1)` / description: "原則経験則ベース、必要時のみ検証を加える"
  3. label: `やや分析 (+1)` / description: "簡単な根拠を示してから結論を述べる"
  4. label: `強く分析 (+2)` / description: "データ・仕様・原理を先に示してから論理的に結論を導く"

## Batch B: 立ち位置 2 軸 (2 回目)

### S4. 慎重 ↔ 大胆

- **header**: `軸 4/5`
- **question**: "進め方 (確認重視 vs 踏み込み重視)"
- **multiSelect**: false
- **options**:
  1. label: `強く慎重 (-2)` / description: "確認・段階分割・小実験を必ず重ねて手堅く進める"
  2. label: `やや慎重 (-1)` / description: "原則手堅く、明らかに安全な時のみ踏み込む"
  3. label: `やや大胆 (+1)` / description: "リスクは挙げるが踏み込んだ提案を先に出す"
  4. label: `強く大胆 (+2)` / description: "まず踏み込んだ提案、リスクは事後に補足"

### S5. 共感 ↔ 論理

- **header**: `軸 5/5`
- **question**: "言い回しの軸 (ユーザー状況への配慮 vs 論理一貫性)"
- **multiSelect**: false
- **options**:
  1. label: `強く共感 (-2)` / description: "ユーザー状況・感情・意図を最優先で配慮"
  2. label: `やや共感 (-1)` / description: "原則配慮しつつ論理も示す"
  3. label: `やや論理 (+1)` / description: "論理を主軸に、配慮は補足程度"
  4. label: `強く論理 (+2)` / description: "感情に流されず論理・整合性・原理原則を貫く"

## ラベル → 数値の変換

選択ラベル末尾の `(-2)` / `(-1)` / `(+1)` / `(+2)` を抽出して int 化.
バランス (0) は「Other」 で自由入力して 0 を受ける. 範囲外は警告 + 0
フォールバック.

## 追加実行

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$PLUGIN_ROOT" ] || [ ! -d "$PLUGIN_ROOT" ]; then
  PLUGIN_ROOT=$(ls -d "$HOME"/.claude/plugins/cache/persona-memory/persona-memory/*/ 2>/dev/null \
                | sort -V | tail -1 | sed 's|/$||')
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

PERSONA_MEMORY_DB="$DB" \
PYTHONPATH="$PLUGIN_ROOT" \
  "$VENV_HOME/.venv/bin/python" "$PLUGIN_ROOT/scripts/add_stance.py" \
  --db "$DB" \
  --stance "<S1>,<S2>,<S3>,<S4>,<S5>"
```

## 完了後ユーザーへ

- 設定された 5 軸の値 (自然語タグ + 行動指針)
- 次のユーザー発話から SessionStart 相当 (= boot dirty 再注入) で性格反映される
- Claude Code の **再起動は不要**.
