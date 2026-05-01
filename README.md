# persona-memory

Local-first persistent memory for Claude Code agents.

- 1 plugin install で `/clear` をまたいで会話の連続性を維持
- 全データは手元の SQLite (sqlite-vec で埋め込み検索) に。外部 API 不要
- 書き込みは軽量 Ollama (gemma3:4b 等)、読み出し圧縮は重量 Ollama (gemma3:12b 等) の二段構え
- ペルソナ設定 (役割・性格・口調・名前) を初回 9 質問で対話的にセットアップ

## 仕組みの一言

| 層 | やること | コスト |
|---|---|---|
| **書き込み (Stop / PreCompact / SessionEnd)** | ユーザー発話 / アシスタント応答 / tool_use / tool_result を **無条件で生保存** + 軽量 LLM が補助的に fact 抽出 | 高頻度発火・低レイテンシ |
| **読み出し (UserPromptSubmit)** | 発話を埋め込み → sqlite-vec で recall → episode 量が大きい時のみ重量 LLM で圧縮してメインエージェントの context に注入 | 必要な時だけ高品質要約 |

詳細設計は `CLAUDE.md` を参照。

## 必要環境

- macOS / Linux (Apple Silicon Mac で開発)
- [Claude Code](https://docs.claude.com/en/docs/agents-and-tools/claude-code/overview)
- Python 3.11+
- [Ollama](https://ollama.com/download) (起動した状態で)
- 推奨ディスク: モデル合計 ~10GB (`gemma3:4b` + `gemma3:12b` + `nomic-embed-text`)

## インストール (推奨: Claude Code plugin)

このリポジトリは Claude Code plugin として配布されています。private repo なので、install には repo の collaborator 権限 + git 認証 (SSH key / `gh auth login`) が必要です。

```sh
# Claude Code 内で
/plugin marketplace add AtsushiShimo/persona-memory
/plugin install persona-memory@persona-memory
```

install 後、Claude Code を完全終了 → 再起動して plugin の hooks と MCP server をロードします。

### 自動更新を有効にする (推奨)

サードパーティ plugin は **デフォルトで auto-update が OFF** です。ON にしておくと、起動時に最新コミットが自動で取り込まれて以降の更新が楽になります:

```
/plugin marketplace               # marketplace 一覧を出して
# (UI 操作で persona-memory の auto-update を有効化)
```

または手動で `~/.claude/plugin-marketplaces.json` 等の設定を編集 (Claude Code のバージョンによって場所が違うので、`/plugin` の subcommand で確認するのが確実)。

### 手動で更新する (auto-update を OFF にしている場合)

```
/plugin marketplace update persona-memory     # 最新コミット fetch
/plugin uninstall persona-memory@persona-memory
/plugin install persona-memory@persona-memory
/reload-plugins
```

**注意**: 単一の `/plugin update` コマンドは現時点で存在しません (Claude Code 仕様)。`/plugin marketplace update` だけだと metadata 更新で止まり、install ツリーが空になる現象を確認済み。確実なのは uninstall → install のサイクルです。

### 初回セットアップ (ペルソナを作る)

plugin install しただけでは「空のペルソナ」状態です。性格を seed する 7 質問を回します:

```
/persona-memory:init
```

Claude が AskUserQuestion ツールでクリック式の選択肢を出します (各問とも自由入力可):

1. 役割 (バックエンドエンジニアの相棒 / 辛口コードレビュアー / 議論パートナー…)
2. 性別
3. 性格 (探究心旺盛 / 冷静沈着・論理的 / 辛口・率直…)
4. 一人称 (僕 / 俺 / 私 / 拙者…)
5. 口調 (敬語 中性的 / 敬語 女性的 / タメ口 男性的 / 武士口調…)
6. ユーザーの呼び方 (あなた / 君 / 〜さん / マスター…)
7. 名前 — Ollama がここまでの設定から 3 案提案、選択 or 自由入力

Ollama モデルは推奨デフォルト (`gemma3:4b` / `gemma3:12b` / `nomic-embed-text`) を使います。
低スペック機で軽量化したい / 大型 GPU で品質を上げたい場合はセットアップ後に
`/persona-memory:configure-models` で変更できます。

完了したら **Claude Code を完全終了 → 再起動**。次回起動から persona facts が SessionStart で自動注入されます。

### 他の slash commands

```
/persona-memory:list                # 登録済みペルソナ一覧 (active 印 + facts/episodes 数)
/persona-memory:status              # 現在のペルソナ状態 + DB stats + Ollama ヘルスチェック
/persona-memory:configure-models    # 軽量/重量モデルを後から変更 (低スペック機向け軽量化など)
```

ペルソナ切替の slash command は意図的に提供していません (セッション中の人格切替は context 混在を起こすため)。複数ペルソナを使い分けたい場合は別ディレクトリに別 install してください。

## インストール (開発・改造したい場合: standalone)

```sh
git clone git@github.com:AtsushiShimo/persona-memory.git
cd persona-memory
scripts/init.sh    # 9 質問の対話セットアップ
```

`init.sh` が以下を全部やります:

- 質問 9 つ
- `.venv` 作成 + Python 依存 install
- Ollama モデル pull (light / heavy / embed)
- `data/<persona>.db` 初期化
- `.mcp.json` 生成 (このディレクトリ専用)
- `data/<persona>.config.env` 書き出し (hooks が読む環境変数)
- `data/active-persona` に persona 名を書く
- 初期 persona facts を seed (役割・性格・口調・名前)

完了後、このディレクトリで `claude` を起動すれば動きます。

### 別ペルソナの派生 (standalone のみ)

```sh
scripts/new-persona.sh <persona-name> <target-dir>
# 例: scripts/new-persona.sh code-reviewer ~/Desktop/code-reviewer-persona
```

別ディレクトリに独立した DB / 性格 / venv のペルソナが切り出されます。

## 使い方の流れ (普通に会話するだけ)

1. Claude Code を起動 (plugin install した後 or standalone のディレクトリで)
2. **普通に会話**: 好み、ルール、属性、振る舞い指示を自然に話す
3. `/clear` してもセッション再起動しても、過去の話は記憶から呼び戻る

エージェントは会話で現れた `preference` / `rule` / `profile` / `skill` / `context` / `persona` 情報を**確認なしで自動保存**します。`write_fact` を意識する必要はありません。

## モデル選択方針 (rule/model_weight_policy)

| 用途 | 既定 | 理由 |
|---|---|---|
| 書き込み (Stop hook 等) | 軽量 (`gemma3:4b`) | 全 assistant ターンで発火するので低レイテンシが効く。生ターンが救命網なので質はそこそこで OK |
| 読み出し圧縮 (proxy_recall) | 重量 (`gemma3:12b`) | recall 量が多い時だけ発火、出力はメインエージェントの context に直接注入されるので品質重視 |

両方とも環境変数 (`PERSONA_LIGHT_MODEL` / `PERSONA_HEAVY_MODEL`) で上書き可能。`init.sh` の質問 1〜2 でも変更できます。

## データ・プライバシ

- 全データは `data/<persona>.db` (sqlite) と `~/.claude-mem/` 等の **ローカルファイルのみ**
- Anthropic に送信される会話は通常通りの Claude Code 経由のみ。記憶用に追加で何か外部サーバーへ送ることはありません
- proxy_recall.py に **シークレット検出** (`sk-...` / GitHub PAT / AWS key 等の正規表現 + 高エントロピートークン) があり、検出した場合はプロンプトを Anthropic に送る前にブロック → ユーザーへ警告
- DB は gitignore 済み、`.mcp.json` も per-host のため commit 対象外

## ファイル構成

```
.claude-plugin/
  plugin.json           plugin manifest (name / version / inline mcpServers)
  marketplace.json      配布カタログ (1 plugin = persona-memory)
hooks/
  hooks.json            5 hooks 宣言 (SessionStart / Stop / PreCompact / SessionEnd / UserPromptSubmit)
.claude/
  settings.json         standalone 用の hooks 登録
  hooks/*.sh            5 hook スクリプト (load_persona_env.sh を source して env 解決)
commands/
  init.md               /persona-memory:init
  list.md               /persona-memory:list
  status.md             /persona-memory:status
scripts/
  init.sh               対話セットアップ (9 質問)
  new-persona.sh        別ペルソナ派生
  load_persona_env.sh   全 hook 共通の env 解決ヘルパ (active-persona → config.env)
  run_mcp_server.sh     MCP server 起動ラッパ
  proxy_recall.py       UserPromptSubmit 時の recall + 圧縮
  auto_persist.py       Stop 時の生ターン保存 + judge fact 抽出
  persist_before_compact.py   PreCompact / SessionEnd 時の生 dump + summary
  gen_greeting.py       SessionStart 挨拶生成
  seed_persona.py       初期 persona facts seed
  init-memory.py        DB schema 初期化
  suggest_names.py      Ollama にペルソナ名候補を提案させる
server/
  main.py               MCP server (FastMCP)
  db.py                 sqlite + sqlite-vec ヘルパ
  embedding.py          Ollama embedding 呼び出し
db/
  schema.sql            DB schema (timestamps は JST = UTC+9)
tests/
  benchmark_scenarios.md   Obsidian 等との比較テストシナリオ集
  test_e2e.py
CLAUDE.md             プロジェクトの設計説明 (Claude エージェント向け)
```

## 既知の制限・トレードオフ

- **mid-session ペルソナ切替なし**: 既に context に注入された persona facts が残るので、切替時は Claude Code 再起動が必要
- **Ollama 必須**: クラウド推論への切替オプションは未実装
- **タイムゾーン**: DB は Asia/Tokyo (JST) 固定。海外ユーザーは schema.sql の `'+9 hours'` を変更
- **シークレット検出**: 正規表現ベースなので未知のトークン形式は素通り。`<private>` タグのような明示マークは未実装

## ライセンス

未定 (private repo の段階)。public 化前に決めます。

## Author

[@AtsushiShimo](https://github.com/AtsushiShimo)
