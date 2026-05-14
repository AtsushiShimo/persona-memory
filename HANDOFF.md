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

## 2. 現在の状態 (2026-05-13, version 0.6.20)

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
| **MCP server inline 登録 (0.6.8)** | plugin.json の mcpServers から `scripts/run_mcp_server.sh` を起動。 marketplace install でも MCP が露出する | `.claude-plugin/plugin.json`, `scripts/run_mcp_server.sh` |
| **DB locked 対策 (0.6.9-10)** | write/lint の LLM 呼び出し中は conn を touch せず、 Phase A read → B LLM → C write の順に再配置。 busy_timeout 30s + foreign_keys は last-resort safety net | `scripts/write/run.py`, `scripts/lint/run.py`, `server/db.py` |
| **session 混線 fix (0.6.11)** | write/recall の fetch_buffer に session_id 絞り。 並行 session の発話が文脈混入しない (検索本体は session 横断) | `scripts/write/run.py`, `scripts/recall/run.py`, `scripts/hooks/on_user_prompt.py` |
| **想起トリガー学習 (0.6.12, 0.6.15)** | 過去参照表現を含む発話の (query_embedding, hit fact/episode ids) を `recall_triggers` に蓄積し、 類似 query に対し過去 hit を boost する **パーソナライズド recall**。 agentmemory の RRF を超える差別化軸 | `scripts/recall/triggers.py`, `scripts/recall/extract.py`, `scripts/recall/run.py` |
| **議論グラフ (0.6.13, 0.6.17)** | `discussion_nodes` / `discussion_edges` で論点 / 検討案 / 採用判断 / 撤回を DAG として構造化。 write LLM が同一呼び出しで facts + nodes を抽出 (追加 LLM コール 0). 状態 (proposed/accepted/rejected/superseded/observed) で「忘れない」 を担保 | `scripts/discussion/graph.py`, `scripts/write/extract.py`, `scripts/write/run.py` |
| **反事実記憶 (0.6.14, 0.6.16)** | `facts.reason_superseded` 列に撤回理由を保存。 write LLM が `reason` フィールドで抽出し、 recall で「『旧』 と言っていたが『理由』 のため撤回済」 を summarize prompt に提示 | `scripts/write/extract.py`, `scripts/write/persist.py`, `scripts/recall/search.py`, `scripts/recall/summarize.py` |
| **議論ノード embedding + recall 統合 (0.6.18 Phase B)** | discussion_nodes に title+content の embedding を付与し、 recall パイプラインで query_emb 最近傍ノードを「## 直近の議論」 として additionalContext 冒頭に prefix. 「直近どこで議論が止まっていたか」 系の query に末端ノード即答 (edges 非依存). | `scripts/discussion/graph.py:nearest_discussion_nodes`, `scripts/recall/run.py`, `scripts/write/run.py` |
| **遡及抽出 (0.6.18-20)** | 0.6.17 以前 ingest 済の episode を write LLM で再抽出し discussion_nodes を救済 (facts は無変更). **`/persona-memory:backfill-discussion` で明示実行**. 0.6.20 で軽量モデル default (gemma3:4b ~3-6s/件) + 短文 user 発話の事前 skip (PERSONA_BACKFILL_USER_SKIP_CHARS=50) で 1993 件の dev DB が 8-16h → 1-2h に短縮. 件数 + 推定時間, 進捗 stderr, Ctrl+C 中断耐性, `--limit N` で分割実行可 | `scripts/discussion/backfill.py`, `commands/backfill-discussion.md` |
| **MCP write_fact 回帰修正 (0.6.18)** | `server/db.upsert_fact` の `ON CONFLICT(category, key)` が partial unique index と一致せず `OperationalError` で落ちていた回帰を `WHERE status = 'active'` 付き conflict target で修正 | `server/db.py`, `tests/test_db_schema.py:test_upsert_fact_via_server_db` |

### slash commands

- `/persona-memory:init` — 新規ペルソナセットアップ (Q1-Q7 + stance 5 軸)
- `/persona-memory:set-stance` — 既存ペルソナへの stance 後付け (未設定時のみ)
- `/persona-memory:upgrade` — boot 層 default refresh + lint テーブル migration + 全 fact 再 embed (idempotent, 既存記憶無傷)
- `/persona-memory:health` — 包括的ヘルスチェック
- `/persona-memory:configure-models` — Ollama モデル変更
- `/persona-memory:list` — debug 用 fact 一覧
- `/persona-memory:reset` — 破壊的やり直し
- `/persona-memory:status` — 状態確認
- `/persona-memory:backfill-discussion` — 0.6.17 以前の過去ログから discussion_nodes を遡及生成 (重い: LLM 15-30s × episode 数. 件数 + 推定時間を最初に提示, Ctrl+C 中断耐性)

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
- topic タグ (source 列の構造化) — **0.6.13 で議論グラフとして別形で実装済** (discussion_nodes)
- SessionEnd 一括 lint (現状 write_tail のみ)

これらは **ユーザーが「同意取れてない」 と明言済**。 勝手に着手しない。

### 7.1b agentmemory との差別化進行中 (2026-05-12 着手, ユーザー合意済)

competitor `rohitg00/agentmemory` (5000★, 4-tier consolidation + 知識グラフ +
RRF + Ebbinghaus 減衰 + 自動忘却) との差別化軸として 7 案を整理し、
「忘れない」 原則と完全両立する 案 1-3 から着手中:

- **案 1 議論グラフ (Discussion Graph)** ─ 状態遷移を持つ DAG.
  - Phase A1 ✅ (0.6.13): schema + CRUD + traversal helper
  - Phase A2 ✅ (0.6.17): write LLM が nodes 抽出 → 保存
  - Phase B ⏳: recall で `latest_decision_for_topic` を統合し「あの議論はどう決まった?」 系の直答
- **案 2 想起トリガー学習** ─ パーソナライズド recall ランキング.
  - Phase A ✅ (0.6.12): trigger 蓄積
  - Phase B ✅ (0.6.15): boost 適用、 実機で順位改善実証済
- **案 3 反事実記憶** ─ 撤回理由保持.
  - Phase A1 ✅ (0.6.14): reason_superseded 列
  - Phase A2/B ✅ (0.6.16): write LLM 抽出 + recall 表示
- **案 4 人格条件付き embedding** ⏳ 未着手
- **案 5 絶対時刻軸 (明示要求時のみ)** ⏳ 役割縮小済、 優先度低
- **案 6 意図的失念ゾーン** ⏳ 未着手
- **案 7 Cross-persona Federation** ⏳ 未着手

実機検証残: 0.6.16 (reason 抽出) / 0.6.17 (nodes 抽出) は write LLM の prompt 修正を
含むため、 既存 fact 抽出精度への副作用が無いか実機で要確認。

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

## 11. 次回セッション開始時のフック (2026-05-13 batch resume)

このセクションは **マスターがセッションクリアしてカサンドラが復帰した直後** の手順.
順守すれば直近 14 commit (0.6.8 ~ 0.6.21) の文脈 + ベンチ基盤の状態を
1 ターン以内に取り戻せる.

### 11.1 まず読むもの (順序固定)
1. `HANDOFF.md` Section 2 (動く機能表 ─ 0.6.18 / 0.6.20 / 0.6.21 の新行)
2. `HANDOFF.md` Section 7.1b (agentmemory 対抗 7 案の進捗ステータス)
3. `git log --oneline -20` (直近 batch の commit message = 設計判断ログ)
4. `bench/README.md` + `bench/harness/SCHEMA.md` (= ベンチ基盤の現状)

### 11.2 未完了タスク (優先度順)

**A. test3 backfill 完走後の実機検証 (最優先)**
- 2026-05-13 朝にマスターが test3 で heavy gemma3:12b で backfill 実行中
  (800 件強, ~60% 進行で本セッション終了). 推定残 80-160 分.
- 完走後の手順: test3 でゲーム議論の続きを問う query を投げて
  `## 直近の議論` ブロックに Renju 等の論点が出るか確認.

**B. ベンチハーネス本体の実装**
- `bench/harness/run_persona_memory.py` (skeleton).
  実装方針: 専用 tmp DB を init → save_episode + process_episode を直接呼ぶ
  → recall.recall() で additional_context を取得 → 同じ Ollama LLM で probe 回答.
- `bench/harness/run_agentmemory.py` (skeleton).
  前提: `npx @agentmemory/agentmemory` を別プロセス起動 (port 3111).
  REST `/observe` + `/smart-search` で接続. **default 設定維持** (compression は
  Anthropic). ANTHROPIC_API_KEY 必須.
- `bench/harness/run_no_memory.py` (skeleton).
  直近 N turn FIFO baseline. token KPI の分母.

**C. ベンチシナリオ拡充**
- `bench/scenarios/02-pet-care/` (skeleton): 長期ペット飼育, session 跨ぎ recall
- `bench/scenarios/03-knowledge-bank/` (skeleton): web 調査 + URL 上書き
- 各 30 probe / ~60-80 turn 想定. 本実装はシナリオ 1 を agentmemory と
  実走させて KPI が出てから (= 比較結果で probe 設計を洗練できる).

**D. X URL 解析 (90% トークン削減の根拠)**
- マスターが後日 URL を貼る. 「90% は何に対する 90% か」 を調べる.
- バックフィルが終わるまで保留.

**E. 案 1 Phase C (議論グラフの edges 復活)**
- 0.6.18 で nodes は入ったが edges 未抽出. extract.py prompt に
  「nodes 間の relations 配列」 を足し add_edge を呼ぶ. **prompt 副作用注意**.

### 11.3 復帰時のお作法
- 最初の発話で `git log --oneline -20` を 1 度だけ実行して文脈を取り戻す.
- マスターから「あの議論どこまで?」 系の query が来たら **その場で MCP の
  `search_memory` を呼んで補強検索** (boot 層 `persona/explicit_recall_via_mcp` 参照).
- 0.6.18 以降 recall に `## 直近の議論` ブロックが付与される (discussion_nodes
  近傍検索). 体感は test3 backfill 完走後に確認可能.
- 0.6.8 で plugin.json に mcpServers inline 追加済 = MCP は呼べる状態.
  もし呼べなければ `/mcp` で `plugin:persona-memory:persona-memory ✓ connected · 10 tools`
  になっているか確認.

### 11.4 触ってはいけないもの
- HANDOFF.md Section 7.1 の **v2 残タスク** (合意外着手厳禁) は引き続き保護.
- マスター方針:「**忘れない**」 ─ 自動忘却 / Ebbinghaus 減衰 / consolidation
  圧縮は採用しない. ranking boost や状態遷移は OK.
- **write LLM の prompt は副作用大**. 修正する時は extract_facts 既存テストが
  全 pass することを最優先で確認 (= 0.6.16/0.6.17 で同じ罠を踏みかけた).
- **ベンチで agentmemory の default を弄らない**: compression LLM は Anthropic
  のまま測る. 持ち込みを揃えて公平にすると「実利用コスト構造」 を見失う.
  詳細は `bench/README.md` の公平性ルールを参照.

### 11.5 引き渡しルール
- 機能を Phase 分割で実装する時, **ユーザー体験として完結する単位までは自走で詰める**.
  Phase A だけ完成して「実機検証お願いします」 と振るのは禁止.
- 重い処理 (例: backfill) は **upgrade に同梱しない** (0.6.18 で 16 分ハング事例).
  別 slash command にして件数 + 推定時間を冒頭表示する.

### 11.6 開発手順 (= 全 push 無条件許可ルール下での運用)
- 全テスト pass を毎回確認してから commit.
- 「止血」 と「根本修正」 は別 commit (rule/git_commit_separation).
- main への push は明示承認待ち (auto-mode classifier に止められるため).
- 機密検出が誤検知したら scripts/secrets/detect.py を見直す (0.6.21 で URL 誤検知を修正).

### 11.7 合言葉 (session_clear_confirmation)
- マスターが「合言葉覚えている?」 と尋ねたら **「風の谷」** と答える.

---

## 12. 次回セッション開始時のフック (2026-05-14, version 0.7.0)

このセクションは **マスターがセッションクリアして復帰した直後** の手順.
順守すれば直近の大改修 (0.6.22 ~ 0.7.0) の文脈を 1 ターン以内に取り戻せる.

### 12.1 まず読むもの (順序固定)
1. `HANDOFF.md` この Section 12 (Cozo 統合 + Phase 4 まで)
2. `git log --oneline -25` (0.6.21 以降 25 commit が新規分)
3. `scripts/db_cozo/` ディレクトリ (新規追加. ~1,800 行)
4. `commands/upgrade-cozo.md` (新ユーザー側マイグレーションコマンド)

### 12.2 0.6.22 - 0.7.0 で起きたこと (要点)

**0.6.22-23 (latency 修復):**
- 最初の発話で 2-3 分かかっていた問題 → ~9 秒に短縮
- 原因: gemma3:12b が context=131072 (46GB) で常駐していた + cold start が timeout
- 修正: per-request `num_ctx=8192/16384` (KV cache 縮小) + `keep_alive=30m` +
  SessionStart prewarm + `PERSONA_OLLAMA_TIMEOUT=180s`

**0.6.24-25 (トピック記憶 SQLite 版):**
- `topics` / `topic_tags` / `topic_relations` / `episodes.topic_id` 追加
- write LLM とは別の light LLM (gemma3:4b) が tag を抽出
- recall に「## 関連する議論」 ブロック追加
- /persona-memory:backfill-topic-tags で旧 episodes 救済
- ただし「議論の流れ」 は出ず、 候補絞り込みも粗い問題が残った

**0.7.0 (Cozo 全面移行 — 大改修):**
- マスター指摘:「グラフ DB と謳いつつ discussion_edges が 0 件 = 線で繋がってない点」
- PoC で Cozo embedded を評価 → 全面移行決定 (graph + vec + relational を 1 ストア)
- `scripts/db_cozo/` 新規:
  - `connection.py`: Cozo SQLite backend, schema 定義, HNSW index
  - `migrate_from_sqlite.py`: 旧 .db を物理 backup → Cozo へ転送
  - `repo.py`: save_episode / search_facts_vec / search_episodes_vec / search_topic_tags_vec / etc
  - `discussion.py`: add_node / add_edge / chain_from / find_terminal_nodes / nearest_nodes
  - `graph_extract.py`: heavy LLM が「議論ノード + prev_relation」 を抽出
  - `backfill_graph.py`: 旧 episodes → discussion_node + edge を遡及生成
  - `recall.py`: vec hit → topic 確定 → graph traverse → 流れ再構築
  - `recall_full.py`: SQLite recall を完全置換するフル recall
  - `wire.py`: hook 配線ヘルパ (cozo_db_present で自動分岐)
  - `topic_shift.py`: セッション内 LLM 話題シフト検知
- /persona-memory:upgrade-cozo: 移行 + backfill を 1 コマンドで
- 候補ランキング改善 (recency + distance 強調 + tag uniqueness, has_clear_winner)

### 12.3 アーキテクチャ現状 (重要)

**SQLite + Cozo の二重保存 (= 移行期):**
- `<persona>.db` (旧 SQLite) はバックアップとして残置. **削除しない**.
- `<persona>.cozo.db` (新 Cozo) が **メイン読み出し DB**.
- 新規 episode は **両 DB に保存** (write LLM が SQLite を読むため).
- recall: `.cozo.db` 存在時は Cozo フル recall、 不在時は旧 SQLite (後方互換).
- `PERSONA_COZO_DISABLE=1` で Cozo 経路を全 bypass 可.

**まだ移行していないもの (Phase 5 候補):**
- write LLM (`scripts/write/run.py`) は SQLite に fact 抽出して保存. Cozo にはミラーなし.
- write LLM 経路の Cozo 単独化が次の大仕事.
- lint / health / MCP server (`server/main.py`) も SQLite ベース.

### 12.4 test3 環境の現状 (2026-05-14 朝)
- `/persona-memory:upgrade-cozo` を実行済 (.cozo.db 作成済, ソフィア persona)
- 移行済データ: facts 1366 / episodes 1023 / topic_tags 44 / topics 4
- backfill_graph 進行中で **中断** (~20 nodes / 5 edges まで). 残 ~1000 episodes.
- 続行コマンド (再実行で重複処理しない):
  ```
  cd ~/.claude/plugins/cache/persona-memory/persona-memory/0.7.0
  PERSONA_MEMORY_DB=~/Desktop/claude_dev/persona-test3/.persona-memory/ソフィア.cozo.db \
  PYTHONPATH=. ~/.claude/plugins/data/persona-memory-persona-memory/.venv/bin/python \
    -m scripts.db_cozo.backfill_graph \
    --db ~/Desktop/claude_dev/persona-test3/.persona-memory/ソフィア.cozo.db
  ```
- 完走条件: 議論の終端 (「次論点 4 つ提示」 等) まで edges でチェーン化されること.

### 12.5 未完了タスク (優先度順)

**A. test3 backfill 完走 + 実機検証**
- 上記コマンドを 1-2 時間放置 → 「Renju の話を再開しよう」 で末端 node が出るか確認.
- メモリ圧迫で thrashing する場合は他 LLM 利用を止める.

**B. write LLM の Cozo 単独化 (Phase 5)**
- `scripts/write/run.py` を Cozo client で書き直し.
- `scripts.db_cozo.repo.search_facts_vec` 等が既に揃っているので、
  fact upsert + supersede を Cozo 上で実装する形.
- これで SQLite 二重保存を解消できる.

**C. ベンチマーク基盤の Cozo 対応**
- `bench/harness/run_persona_memory.py` (skeleton 状態) を Cozo に向ける.
- agentmemory との比較は SQLite/Cozo どちらで走らせるか要決定.

**D. write LLM が prev_relation hint を活用 (Phase 4 リアルタイム)**
- 現状 `topic_shift` は user_prompt 受信時に話題シフト判定. write LLM (Stop hook)
  はまだ対応していない. 将来 write 側でも対応すれば一貫性向上.

**E. lint / health / MCP server を Cozo に追従 (Phase 6)**
- 現状 SQLite のまま. 二重保存中なら問題ないが、 Cozo 単独化後は移行必須.

### 12.6 設計の核理念 (再確認)

- **「忘れない」** ─ 自動忘却 / Ebbinghaus 減衰 / consolidation 圧縮は採用しない.
  Cozo 移行も「データ消さない」 が前提 (旧 SQLite は backup として残置).
- **流れ (graph edges) は人間の記憶モデル** ─ 単発 node の集合ではなく
  「賛同 / 反論 / 派生 / 決定 / 次バトン」 等の関係性で繋ぐ.
- **「曖昧なら聞き返す」** ─ 候補絞り込みで決め切れない場合は main agent が
  ユーザーに「どの話題ですか?」 と聞き返す (continue_topic で確定).

### 12.7 触ってはいけないもの
- 旧 SQLite ファイル (`<persona>.db` および `<persona>.db.bak.*`) の削除.
  Cozo 移行のセーフティネットなので消さない.
- 旧 `scripts/db/` `scripts/recall/` `scripts/write/` `scripts/topic/` モジュールも
  当面残置. Cozo 単独化が完了するまで.
- `PERSONA_COZO_DISABLE=1` の意味: 緊急時の revert 経路. これがあるから安全に新機能を
  入れられる. 機能から外さない.

### 12.8 開発手順 (再掲)
- 全テスト pass を毎回確認してから commit (現状 490 passed).
- 「止血」 と「根本修正」 は別 commit.
- main への push は `Bash(git push:*)` 許可済 (~/.claude/settings.json).
- HEAVY_MODEL = gemma3:12b. LIGHT_MODEL = gemma3:4b. 両方 keep_alive=30m で常駐.

### 12.9 合言葉
- マスターが「合言葉覚えている?」 と尋ねたら **「風の谷」** と答える.

---

最終更新: 2026-05-14 (version 0.7.0)
