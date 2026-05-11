# persona-memory 引き継ぎ資料

このファイルは、 開発を継続する別ペルソナ / 別セッション向けの **現状サマリ + 守るべき原則** を 1 枚に集約したもの。  
細かい設計議論の経緯は git log (`git log --oneline`) と `CLAUDE.md` を参照すること。

---

## 1. プロジェクトの存在意義 (北極星)

ユーザー (志茂) が AI 利用で抱える **2 つの核心課題** を解決するためのプラグイン:

1. **セッションごとに記憶を失う** → ナレッジが蓄積されない
2. **エージェントに固有の記憶や考えがない** → 行動パターンに囚われ視野狭窄に陥る (= 別の解決策があっても辿り着けない)

採用した解決軸:

- **記憶を積み上げ、個性・性格を擬似構築** (= 1 親エージェント = 1 ペルソナ = 1 DB)
- **別 LLM も同じフィールドで会話させて視野狭窄を防止** (= 立ち位置 5 軸で意図的に思考傾向を散らす)

→ 全機能はこの北極星に従属する。 「あったら便利」 では機能を入れない。

---

## 2. 現在の状態 (2026-05-11, version 0.5.25)

### 動く機能

| 機能 | 概要 | 関連 |
|---|---|---|
| **raw save** | UserPromptSubmit / Stop で全発話を episodes に同期保存 | `hooks/hooks.json`, `scripts/hooks/on_user_prompt.py` |
| **write LLM** | 発話 → heavy LLM (gemma3:12b) が fact 抽出 → DB に supersede / insert | `scripts/write/run.py`, `extract.py` |
| **recall** | UserPromptSubmit で発話全文を embed → vec0 cosine 検索 → summarize LLM が curate → additionalContext で main agent に注入 | `scripts/recall/run.py`, `search.py`, `summarize.py` |
| **lint** | write 完了後の tail で新 fact 近傍を judge LLM (heavy) で矛盾検査 → auto_resolve / flag | `scripts/lint/run.py` |
| **boot 層** | SessionStart で persona / rule の active facts を全件注入 (playbook_* は除外) | `scripts/boot/inject.py`, `defaults.py` |
| **エスカレーション** | 長文 / 不確実 / 高 importance で claude -p 子プロセスに丸投げ | `scripts/escalate/claude_p.py` |
| **機密フィルタ** | 両 hook 入口で API キー等を検出して block | `scripts/secrets/detect.py` |
| **デバッグモード** | `PERSONA_MEMORY_DEBUG=c` で recall パイプラインを全段ログ | `scripts/debug/recall_log.py` |
| **health check** | DB / 設定 / Ollama / 直近 ingest 健全性を 1 関数で集計 | `scripts/health.py` |
| **未処理 episode 再抽出** | SessionStart で write が詰まった episode を再開 | `scripts/resume.py` |
| **lint の recall 接続** | source='lint_conflict' な supersede の旧 value を「過去には〜と言っていたが撤回済」 として補足表示 | `scripts/recall/search.py`, `summarize.py` |
| **playbook (条件付きノウハウ)** | category=persona + key prefix `playbook_*`. SessionStart 注入除外、 dynamic recall でベクター hit | `scripts/write/extract.py`, `boot/inject.py`, `recall/search.py` |
| **立ち位置 5 軸** | init 時 + 後付けで `persona/stance` に自然語形式で記録 | `scripts/seed_persona.py`, `add_stance.py` |
| **MCP search_memory 条件付き許可** | 自動 recall で物足りない時のみ main agent が意図的に呼んで補強検索 | `boot/defaults.py` の `persona/explicit_recall_via_mcp` |
| **外部ナレッジ保存** | web 調査 (WebFetch / web-page-reader / x-post-reader) 結果を構造化して category=`knowledge` で保存。 同 URL 再調査で上書き、 別 URL は並存 | `server/main.py:save_knowledge`, `boot/defaults.py:playbook_after_web_research` |

### slash commands

- `/persona-memory:init` — 新規ペルソナセットアップ (Q1-Q7 + stance 5 軸)
- `/persona-memory:set-stance` — 既存ペルソナへの stance 後付け (未設定時のみ)
- `/persona-memory:upgrade` — boot 層 default refresh + lint テーブル migration + 全 fact 再 embed (idempotent, 既存記憶無傷)
- `/persona-memory:health` — 包括的ヘルスチェック
- `/persona-memory:configure-models` — Ollama モデル変更
- `/persona-memory:list` — debug 用 fact 一覧
- `/persona-memory:reset` — 破壊的やり直し
- `/persona-memory:status` — 状態確認

### MCP tools (server/main.py)

- `write_fact` / `search_memory` / `lint_memory` / `forget_fact` / `delete_fact` / `append_episode` / `list_facts` / `health_check`

---

## 3. 設計の核となる原則 (絶対に守る)

これらは過去に何度かの失敗を経て確立されたもの。 **逸脱すると同じ問題を再発させる**。

### 3.1 実装ルール

- **最小限・シンプル** が大原則。 1 問題 1 修正。 regex / 新コマンド / プロンプト改修を抱き合わせない。 (`memory/feedback_minimal_implementation.md`)
- **shotgun 禁止**. 原因不明で複数仕掛けを並べるのは最悪。 仮説は 1 つずつ検証。 (`memory/feedback_no_shotgun_diagnosis.md`)
- **中途半端な引き渡し禁止**. 動作未確認 / テスト未通過の状態で commit / 報告しない。 各段階で実機 e2e + 既存テスト全通過してから次へ。 (`memory/feedback_no_half_baked_handover.md`)
- **ピンポイント編集**. 指示外の場所のリファクタ / 整理 / コメント追加 / フォーマット変更は禁止。 「ついで作業」 はやらない。 (`~/.claude/CLAUDE.md`)

### 3.2 アーキテクチャ原則

- **記憶は会話の中で自然に蓄積される** → 報告しない。 「記憶します」 「記憶しました」 等は禁止 (`boot/defaults.py:silent_memory`)。
- **内部用語を出さない**. recall / write / embedding / sqlite / hook / fact / importance 等の技術用語をユーザー応答に出さない。 自然語で「思い出す / 覚えている / 記憶にない」 等に翻訳 (`boot/defaults.py:natural_voice`)。
- **応答は端的に**. 前置き・状況再確認・枕詞を省く。 長文禁止 (`boot/defaults.py:response_brevity`)。
- **確認が先、 実行が後**. 疑問形は提案であって指示ではない。 ユーザーの明示承認 (「お願い」「進めて」 等) を待つ (`boot/defaults.py:confirmation_before_acting`)。
- **recall は全発話で走らせる**. 「OK」「うん」「ありがとう」 でも skip 禁止。 (`memory/feedback_no_recall_skip.md`)

### 3.3 プラグイン操作

- **plugin cache を直接いじらない**. `~/.claude/plugins/cache/` 配下を git pull / checkout / 編集禁止。 改善は dev リポで完結し、 ユーザーに `/plugin install` 等の操作を依頼。 (`memory/feedback_no_cache_modification.md`)
- **tool 呼び出しを最小化**. Bash / Read / Edit 連発を避け、 集約・並列・回避する。 ユーザー体感速度に直結。 (`memory/feedback_tool_call_thrift.md`)

### 3.4 機密フィルタ

- **write 側にも置く**. recall 側だけだと記憶時にフィルタかからず指示違反。 (`memory/feedback_secret_filter_placement.md`)

---

## 4. アーキテクチャ概要

### 4.1 2 層構造

- **boot 層** (`persona`, `rule`) — SessionStart で全件注入 (= 性格・常時ルール)
  - 例外: `key LIKE 'playbook_%'` は注入除外、 dynamic recall でのみ hit
- **dynamic 層** (`preference`, `aversion`, `profile`, `skill`, `context` + playbook) — UserPromptSubmit で発話に関連するものだけ proxy 経由で注入

### 4.2 hook フロー

| Hook | 同期処理 (出口で必ず完了) | 非同期 (detach) |
|---|---|---|
| `UserPromptSubmit` | raw save → recall → additionalContext 出力 | write LLM 起動 |
| `Stop` | assistant 応答 raw save | write LLM → lint LLM (tail) |
| `SessionStart` | boot 層注入 / 未処理 episode 再抽出 detach | (重い初期化があれば detach) |
| `PreToolUse` | DB 直接アクセス / auto-memory ブロック | - |

### 4.3 3 LLM 並列

- **write** (heavy, default `gemma3:12b`) — fact 抽出
- **recall** (heavy, default `gemma3:12b`) — analyze_query + summarize
- **lint** (heavy, default `gemma3:12b`) — judge_conflict
- **embed** (`nomic-embed-text`)

`PERSONA_WRITE_BACKEND=claude` で write のみ `claude -p` 子に切替可能 (= LLM 性能 vs プロンプト/DB 設計 の切り分け診断用)。

### 4.4 ベクター検索

- **fact_embeddings / episode_embeddings**: vec0 cosine distance
- **embed text 形式**: `<category>/<key>: <value>` (boot 層も write 経路も統一)
- **distance_max**: 0.6 (recall) / 0.4 (lint neighbor)
- **検索キー**: 発話全文 1 つを embed (= keyword 個別 embed は OOV collapse で破綻するため廃止済み)

### 4.5 重要な防御ヒューリスティック

- **`_is_same_attribute(key_a, key_b)`** — key 末尾単語が同じなら同属性、 違えば別属性 (= `pet_dog_name` vs `pet_dog_breed` は別物として扱う)
- **`_strip_dup_suffix`** — seen_keys 由来の `_2` / `_3` を剥がしてから末尾比較
- **lint は同 category + 同属性のみ judge** — 異 category / 異属性の誤判定暴走を防止
- **write の seen_keys セーフティネット** — LLM が同 batch で同 key を出した時に `_2` / `_3` suffix で情報損失を防ぐ
- **supersede 時に旧 embedding を削除** — vec0 knn 枠を active 外の embedding が喰う問題を解消

---

## 5. 開発ワークフロー

### 5.1 dev リポ と 利用プロジェクトの分離 (重要)

**dev リポ (このディレクトリ) では plugin を install しない**。 衝突するため:

- hook 経由の write LLM が dev 議論を fact 化 (= 「lint 暴走を直した」 等)
- PreToolUse hook が dev 中の `sqlite3 .persona-memory/x.db` を自己ブロック
- heavy LLM の Ollama 占有で dev レスポンス遅延

**正しいモデル**: 別プロジェクト (例: `~/Desktop/claude_dev/persona-dev-pilot/`) を新規作成し、 そこで `/plugin install` + `/persona-memory:init`。 そのペルソナに「`~/Desktop/claude_dev/persona-memory/` を編集して」 と指示する。

### 5.2 テスト

```bash
cd ~/Desktop/claude_dev/persona-memory
PYTHONPATH=. .venv/bin/python -m pytest tests/ --tb=short
```

現状 230+ 件、 全通過が前提。

### 5.3 実機 e2e の手順

`scripts/tools/recall_replay.py` を使えば、 既存 DB に対して recall パイプラインを Claude Code 再起動なしで試せる:

```bash
PYTHONPATH=. .venv/bin/python scripts/tools/recall_replay.py \
  --db ~/Desktop/claude_dev/persona-test*/.persona-memory/<name>.db \
  --query 'テストしたい発話'
```

write は detached child なので hook 経由で投入する必要がある。 fresh DB で「コーヒーは深煎り派、 砂糖なし」 → 「やっぱり浅煎り」 → ... のような会話シナリオを流して挙動を観察するのが定番 (過去 commit 例: 0.5.13 / 0.5.17 / 0.5.18 / 0.5.19 の commit msg 参照)。

### 5.4 リリース手順

1. 変更 + tests pass + 実機 e2e 完走確認
2. `.claude-plugin/plugin.json` の `version` bump
3. `git commit` (commit msg に変更理由 + 実機検証結果を必ず記載)
4. `git push origin main`
5. ユーザーに「`/plugin marketplace update persona-memory` + `/reload-plugins`」 の作業依頼

---

## 6. やってはいけないこと (= 過去にやらかしたパターン)

- **lint で異 category / 異属性の fact を judge する** → 暴走して関連 fact を auto_supersede で消滅 (0.5.16 / 0.5.17 で修正)
- **find_match の embedding fallback で異属性を同一視する** → 「まろん / ミニチュアダックス / オス」 を全部 pet_dog_name で supersede (0.5.17 で `_is_same_attribute` 追加)
- **write LLM が buffer の古い発話を再抽出する** → 「やっぱり浅煎り」 後に「深煎り」 が復活 (0.5.18 で prompt 強化)
- **value 単独で embed する** → nomic-embed-text の OOV collapse で「糖尿病」「MVP」 等が同一 embedding に化ける (0.5.7 で `<cat>/<key>: <value>` 形式に統一)
- **recall を「相槌だから skip」 する** → 「OK」「うん」 で過去議題が消える (0.5.9 で skip ロジック撤廃)
- **未確認の状態で commit する** → 「claude backend 経由は unit test だけ pass、 実機未確認」 で commit したケース (0.5.13)。 ユーザーから「中途半端で持ってくるな」 と指摘あり

---

## 7. 既知の限界・将来課題

### 7.1 ユーザー合意済 v2 残 (合意外 = 着手厳禁)

- `/persona-memory:condense` — boot 層が肥大化した時の要約統合
- `/persona-memory:allow-last + secret-allowlist` — 機密誤検出時の上書き
- 3 並列 Ollama daemon 最適化
- topic タグ (source 列の構造化)
- SessionEnd 一括 lint (現状 write_tail のみ)

これらは **ユーザーが「同意取れてない」 と明言済**。 勝手に着手しない。

### 7.2 write LLM の品質課題 (= LLM 任せの部分)

- 属性の自動細分化は prompt で誘導しているが、 LLM の判断次第。
- 「深煎り、 砂糖なし」 を `coffee_roast` + `coffee_sugar` に分けるか、 1 つの `coffee_preference` にするかは LLM 次第。
- 改善は prompt or backend (Claude) 切替で。

### 7.3 nomic-embed-text の弁別力

- 短い日本語句で cos sim が 0.7-0.9 帯に圧縮されて関連性判定が弱い。
- `<cat>/<key>: <value>` 形式の embed text で多少救えるが、 完璧ではない。
- 検索結果の最終 curate は summarize LLM (gemma3:12b) に委ねる設計。

---

## 8. ディレクトリ構成

```
persona-memory/
├── .claude-plugin/plugin.json       # plugin manifest (version はここ)
├── .claude/hooks/                    # hook entry shell scripts
├── CLAUDE.md                         # プロジェクト指示書 (ペルソナ親エージェント向け)
├── HANDOFF.md                        # このファイル
├── commands/                         # slash command 定義 (init / health / etc.)
├── hooks/hooks.json                  # hook 登録 (UserPromptSubmit / Stop / etc.)
├── scripts/
│   ├── db/                           # schema / migrate / connection
│   ├── boot/                         # boot 層注入 + DEFAULT_BOOT_FACTS
│   ├── write/                        # write LLM (extract / persist / similarity / run)
│   ├── recall/                       # recall (extract / search / summarize / run)
│   ├── lint/                         # lint LLM (judge_conflict / run)
│   ├── secrets/                      # 機密フィルタ
│   ├── escalate/                     # claude -p 子プロセス
│   ├── hooks/                        # hook Python entry points
│   ├── shared/                       # ollama / embedding ヘルパ
│   ├── debug/                        # debug log
│   ├── tools/                        # recall_replay 等 dev tool
│   ├── seed_persona.py               # init 時の persona facts seed
│   ├── add_stance.py                 # 既存 DB への stance 追加 (未設定時のみ)
│   ├── upgrade.py                    # boot 層 default refresh + migration
│   └── health.py                     # 包括 health check
├── server/main.py                    # MCP server (FastMCP)
├── tests/                            # 230+ unit tests
└── setup.sh                          # venv + Ollama モデル + DB init
```

主要モジュールの責務は冒頭 docstring を読むこと。

---

## 9. デバッグ手段

| 用途 | コマンド |
|---|---|
| recall 全段を offline で再現 | `python scripts/tools/recall_replay.py --db X --query Y` |
| 包括ヘルスチェック | `python -m scripts.health` (env `PERSONA_MEMORY_DB=path/to.db`) |
| facts 一覧 | `sqlite3 X.db "SELECT id, category, key, status, substr(value,1,60) FROM facts ORDER BY id"` |
| write LLM の挙動を直接観察 | `python -c "from scripts.write.extract import extract_facts; ..."` (test10 で実証済の手法) |
| recall debug log | `PERSONA_MEMORY_DEBUG=c` を hook 起動環境に渡す |

---

## 10. 引き継ぎ先への期待

- このファイル + `CLAUDE.md` + `~/.claude/projects/<this>/memory/MEMORY.md` の 3 点セットを読めば、 現状コードに着手できる粒度になっている。
- 議論履歴の細部は **git log の commit message に必ず記載済** (= 「なぜそうしたか」 + 「実機検証結果」 がペアで残っている)。 commit を遡れば過去の試行錯誤が辿れる。
- 新機能の合意は **ユーザーに必ず先に確認**。 v2 残タスクには「合意外 = 着手厳禁」 の項目がある (Section 7.1)。
- 引き継ぎ後にこのファイル自体が古くなったら都度更新する。

---

最終更新: 2026-05-11 (version 0.5.25)
