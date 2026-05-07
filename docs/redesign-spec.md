# persona-memory 再設計仕様書

最終更新: 2026-05-07
ステータス: 議論中 (実装未着手) — 議論で確定した事項を逐次追記する。

---

## 0. 背景

既存実装は積み上げの過程で「ユーザーが認識していない仕様誤認」 が複数混入している (例: 機密フィルタを recall 側のみに置いた件)。継ぎ足し改修では除去できないため、**ゼロベースで再構築** する方針。

---

## 1. 北極星 (3 原則)

このプラグインが解こうとしているのは「AI 利用における記憶非蓄積問題」 — 会話の積み重ねが残らず、過去の決定や好みが失われること。

ゆえに以下を原則とする:

1. **自動的に**: ユーザーが「覚えて」 と言わなくても記憶される
2. **全ての会話が**: 取捨選択せず全発話を記憶対象とする
3. **記憶される**: 一定期間で消えない (= TTL ベースの忘却は採らない)

---

## 2. ライフサイクル方針

### 2.1 忘却しない

- 「3 ヶ月で忘れる」 等の TTL 削除はコンセプト違反
- HDD 容量が許す限り保持する
- 古さは **削除ではなく recall 時の優先度低下で表現** する

### 2.2 ハード面の懸念は後回し

容量・メモリ・並列起動コスト等の心配は、テスト中に問題化したら対策する。設計議論段階では考慮外。

---

## 3. アーキテクチャ: ローカル LLM 3 分割

ローカル LLM を **3 つの役割で並列稼働** させる (DB のマスター/スレーブ的な責務分離):

| 役割 | 主責務 |
|---|---|
| **write** | 発話から fact 抽出、類似 fact の検出と上書き、DB 永続化 |
| **recall** | 発話のキーワード/意図抽出、DB 検索、記憶付帯 prompt の構築、要約 |
| **lint** | 矛盾検出、整合性チェック (write 完了直後にピギーバック + SessionEnd で一括パス、§6) |

### 3.1 エスカレーション

ローカル LLM はサポート役。以下の **OR 条件** のどれか 1 つでも満たせば Claude にエスカレーション:

1. **長文・複雑性**: ローカル LLM への **入力トークン総量 > 2000** (ユーザー発話単独ではなく、バッファ + 検索結果 + プロンプト含む全体)
2. **不確実性**: ローカル LLM が判定不能を返した時
   - write: 「補強か変更か区別できない」
   - recall: 「現発話の意図が解釈できない」
   - lint: 「両立不能、どちらが正か裁定不能」
3. **重要度**: `importance >= 8` の fact を上書き / 削除する操作 (persona / rule クラスの根幹を触る時)

閾値 (2000 トークン / `importance >= 8` / `BUFFER_N = 3`) は調整可能パラメータとして持つ。エスカレーション時は必ずログに残し、閾値調整の根拠にする。

### 3.2 並列性と整合性

- write は非同期で OK (DB 永続化は遅延 OK)
- recall は即時応答が必要 → 「直近会話バッファ + DB 検索結果」 を合成して eventual consistency を解く
- 直近 `BUFFER_N` 発話を recall 入力に毎回混ぜる (実体は episodes テーブル末尾の SELECT、専用 in-memory テーブルは作らない — 詳細は §9.5)

---

## 4. write 仕様

### 4.0 トリガー

**全発話**。3 原則「全ての会話が記憶される」 から自動的に導かれる。例外なし。

### 4.1 最重要責務: 類似 fact の上書き

新しい情報が来たとき、既存の類似 fact を **両方残さず一本化** する。これが write の最も重く重要な処理。

### 4.2 類似判定 = ハイブリッド

- key 厳密一致を最初にチェック
- ヒットしなければ embedding 距離で近傍探索
- 両方のゲートを通すことで、表記揺れと別概念の誤合体の両方を防ぐ

### 4.3 旧版 fact の扱い: supersedes チェーン

- facts に `status: active | superseded` 列を持たせる
- 新 fact は `supersedes: <旧fact_id>`、旧 fact は `superseded_by: <新fact_id>` で双方向リンク
- recall の default 検索は `status = active` のみ → ノイズ化を防ぐ
- 「以前はこう言ってた」 等の履歴参照を recall LLM が検知したときだけ chain を遡って archive を取得
- **補強** (同じ事実の言い直し) と **変更** (値が変わった) を区別
  - 補強 → importance / access_count を加算、value 更新のみ、archive 化しない
  - 変更 → supersedes リンクを張って旧 fact を archive 化
- 区別が難しい場合は Claude にエスカレーション

### 4.4 並列 write の競合制御

- 方式: **last-write-wins**
- 理由: 「並列」 とは連投発話で前の write が終わる前に次インスタンスが立ち上がるケース。順序が会話時間で確定しているので、後の方が新情報。ロック・キュー・judge は不要

### 4.5 機密フィルタ

機密検出 (API キー / 高エントロピー文字列など) は両 hook (UserPromptSubmit / Stop) の **入口で同期実行**。検出時は raw 保存 / fact 抽出 / recall を **すべてスキップ** = 一切永続化しない・LLM に流さない。

- フィルタ自体は regex + エントロピー計算 (ミリ秒オーダー)、速度劣化は無視できる
- assistant 応答側 (Stop hook) でも同じチェックを通す (ユーザー発話を引用したケース等を防ぐ)
- 機密以外 (愚痴・性格・嗜好) はフィルタせず記憶対象にする — エージェントが人間のパートナーのように成長するため

#### 検出時の挙動

- **警告**: stderr に「機密検出、永続化しない」 + 検出箇所 (マスク済) を出力
- **Keychain 退避ガイダンス**: 警告メッセージに「`.env` の MY_KEY に入れて、会話では『.env の MY_KEY』 と参照」 という安全な手順を案内
- **override 手段** (2 段階):
  - (a) スラッシュコマンド `/persona-memory:allow-last` — 直近 1 ターンを「機密ではない」 と承認、対応 episode を retro 保存 + write をキック
  - (b) 設定ファイル `.persona-memory/secret-allowlist` — regex パターンを恒久的に除外 (例: `^test_token_` を許可)

---

## 5. recall 仕様

### 5.1 設計フロー (LLM インターセプト方式)

1. ユーザー発話を recall LLM がインターセプト
2. 発話 + 直近会話バッファから **キーワード/意図を抽出** (短発話・指示語・代名詞を解決)
3. 抽出結果で DB を検索 (default は `facts WHERE status='active'` のみ。episodes / supersedes chain は §10 のトリガー条件下でのみ)
4. **ユーザーの発話自体は改変せず**、関連記憶を付帯した prompt をメインエージェントに渡す
5. メインがそれを読んで「思い出しながら話す」 形で応答

### 5.2 何が壊れていたか

現行実装は「発話をそのまま embedding して検索」 しており、LLM がクエリを理解する工程が抜けていた。これだと:

- 「OK」「了解」 のような短発話で query が痩せる
- 「さっきの話」 のような指示語が解決されない
- 意図と表層文字列のズレを吸収できない

### 5.3 入力の構成

- 現発話 + **直近 `BUFFER_N` 発話のバッファ** (指示語解決のため)
- `BUFFER_N` は **暫定 3** でテスト開始、精度を見て調整

### 5.4 優先度の重み式

`relevance × recency × importance × access_count`

- relevance: 検索クエリとの距離
- recency: 新しいほど重み大
- importance: write 時に付与した重要度
- access_count: 過去に呼ばれた回数 (頻出記憶は沈まない)

古いものでも重要 / 頻繁に呼ばれるものは沈まない設計。

### 5.5 summarize は recall の役目

要約 (会話圧縮、長文記憶の整形) は recall 側の責務。write には持たせない。

---

## 6. lint 仕様

- 主トリガー: write 完了直後にそのプロセスから lint LLM を tail 起動 (直近 fact だけ集中チェック、軽い)
- 副トリガー: SessionEnd で大量パス (古い矛盾も拾う、重い)
- cron / launchd は使わない (シェル側に外部依存を作らない)
- 自動解消できるものは flag → auto-resolve
- 重要な裁定は Claude にエスカレーション

(詳細ロジックは今後議論)

---

## 7. デバッグモード

### 7.1 トグル

- 環境変数 `PERSONA_MEMORY_DEBUG=1` (or レベル指定 `a|b|c`)
- ON 中は毎 recall で毎回出す

### 7.2 粒度 (default = c)

- (a) 抽出キーワード/意図のみ
- (b) + DB 検索ヒット (距離・importance 含む)
- (c) + メインに渡す最終 prompt 全文 ← **default**

### 7.3 出力先

- `stderr` + ログファイル (`data/debug-recall.log`) の併用
- **`additionalContext` には入れない** (Claude 本体への注入文を debug 情報で汚さない)

### 7.4 なぜ必要か

recall の中身が見えないと精度検証ができない。「旧説/新説の認識精度」 や「キーワード抽出の妥当性」 を判断するには (c) まで見える必要がある。

---

## 8. 記憶の層構造とカテゴリ

性格・本能と経験・記憶を **2 層** で扱う。両層とも write の対象 = 成長する。

| 層 | カテゴリ | 注入経路 | 意味 |
|---|---|---|---|
| **boot 層** | `persona`, `rule` | SessionStart で全件注入 | 性格・本能。起動時に常駐し、自分の一部として手元にある (recall されない) |
| **dynamic 層** | `preference`, `aversion`, `profile`, `skill`, `context` | UserPromptSubmit で関連分のみ recall | 経験・記憶。動的に引き当てる |

「persona は記憶ではない (= 性格)」 という直感を保ちつつ、会話を通じて persona/rule も追加・上書きされる (= 成長する) — これを **注入タイミングの違い** として表現する。新カテゴリを追加する際は「boot か dynamic か」 を最初に決める。

### 8.1 boot 層の即時反映

会話中に write LLM が boot 層 (persona/rule) に新規 fact を書いたら、**次の UserPromptSubmit hook で boot 層の最新を再注入** する。Claude Code の SystemPrompt は途中で書き換えできないため、厳密には「次発話以降に反映」 となる。これで「同セッション内で書いた性格が即座に効く」 を実用的に成立させる。

### 8.2 boot 層の容量管理: `/persona-memory:condense` コマンド

boot 層が肥大化すると SessionStart の注入トークンが context window を圧迫する。対策:

- スラッシュコマンド `/persona-memory:condense` を提供
- 動作: boot 層の fact を importance / access_count / recency で並べ、低価値・冗長なものを Claude エスカレーションで要約統合 (= boot 層の自己整理)
- **自動実行はしない** (重要記憶を勝手にいじらない)、ユーザーが手動で叩く
- 提唱トリガー: SessionStart で以下のいずれかを満たしたら警告
  - boot 層 fact 数 > **50** (暫定)
  - boot 層合計トークン数 > **5000** (暫定)
- 閾値はテストで調整

---

## 9. データスキーマ

### 9.1 facts (構造化記憶の本体)

| 列 | 型 | 説明 |
|---|---|---|
| `id` | INTEGER PK | |
| `category` | TEXT | persona / rule / preference / aversion / profile / skill / context |
| `key` | TEXT | カテゴリ内の識別子 |
| `value` | TEXT | 内容本体 |
| `importance` | INTEGER (1-9) | 重要度。エスカレーション閾値に使う |
| `access_count` | INTEGER | recall で引かれた回数 |
| `status` | TEXT | `active` or `superseded` |
| `supersedes` | INTEGER (FK→facts.id, nullable) | 置き換えた旧 fact |
| `superseded_by` | INTEGER (FK→facts.id, nullable) | 置き換えた新 fact |
| `source` | TEXT (nullable) | URL / "conversation" / 取り込み元 |
| `created_at` | TIMESTAMP | |
| `updated_at` | TIMESTAMP | 補強で更新 |
| `last_accessed_at` | TIMESTAMP | recency 計算用 |

**制約**: `UNIQUE(category, key) WHERE status = 'active'` — active 層は category+key で一意 (last-write-wins を物理層で担保)

※ session_id 列は YAGNI のため持たせない。必要になったら ALTER TABLE で追加する。

### 9.2 fact_embeddings (sqlite-vec の vec0 virtual table)

| 列 | 型 | 説明 |
|---|---|---|
| `fact_id` | INTEGER (FK→facts.id) | |
| `embedding` | BLOB (vec) | nomic-embed-text 等 |

superseded fact の embedding も保持 (履歴 chain を辿るときに使う)。

### 9.3 episodes (会話ログ)

| 列 | 型 | 説明 |
|---|---|---|
| `id` | INTEGER PK | |
| `role` | TEXT | `user` / `assistant` |
| `content` | TEXT | 生発話 (zero-loss 保証) |
| `summary` | TEXT (nullable) | recall LLM が生成 |
| `session_id` | TEXT | 未処理 episode の再抽出単位として使う |
| `timestamp` | TIMESTAMP | 直近 `BUFFER_N` バッファの抽出元 |

**status / supersedes 列は不要** (時系列でそのまま残す)。

### 9.3a episode_embeddings (sqlite-vec の vec0 virtual table)

| 列 | 型 | 説明 |
|---|---|---|
| `episode_id` | INTEGER (FK→episodes.id) | |
| `embedding` | BLOB (vec) | content or summary を embed |

### 9.4 escalation_log

| 列 | 型 | 説明 |
|---|---|---|
| `id` | INTEGER PK | |
| `timestamp` | TIMESTAMP | |
| `reason` | TEXT | `long_input` / `uncertainty` / `high_importance` |
| `input_size` | INTEGER | トークン数 |
| `caller` | TEXT | `write` / `recall` / `lint` |
| `outcome` | TEXT | 成功/失敗、結果サマリ |

→ 閾値 (2000 トークン / importance=8) 調整の根拠データ。

### 9.5 設計判断

- **facts と episodes を分離**: facts は抽出済み構造化情報 (上書き対象、active 制約あり)、episodes は生会話 (zero-loss、時系列)。同居させると active 制約が壊れる
- **superseded fact も同じ facts テーブル**: 別 archive テーブルは作らない。status で絞るだけ
- **優先度はクエリ時計算**: recency が時間経過で変わるので保存しない
- **conversation buffer (`BUFFER_N`)**: episodes の末尾を SELECT で取る。in-memory 専用テーブルは作らない

---

## 10. recall の検索範囲とトリガー

### 10.1 検索範囲

- **default**: `facts WHERE status='active'` のみ
- **episodes も検索**: 「あの時こう言ってた」「先週の話」 等の会話引き戻しを recall LLM が検知 or ユーザー明示指示があったとき
- **superseded facts (履歴 chain)**: §10.2 のトリガーで遡る

→ 常時 2 テーブル検索を走らせない。default は最小範囲、必要時に拡張。

### 10.2 履歴 chain を辿るトリガー

ハイブリッド:
- **明示指示**: ユーザー発話に「以前」「昔は」「変える前は」 等のキーワード or recall LLM がそれと判定
- **LLM 検知**: recall LLM が「現発話の意図には履歴が必要」 と判定
- 履歴 chain は最大 `CHAIN_DEPTH` 段まで遡る、それ以上は要約 (`CHAIN_DEPTH` はテストで決定)

---

## 11. Hook 配置と発火タイミング

3 役割 (write / recall / lint) × 5 hook の責務分担:

| Hook | 同期処理 (出口で必ず完了) | 非同期処理 (detach で継続) |
|---|---|---|
| `UserPromptSubmit` | ① 機密チェック (検出時は以下を全部スキップ、§4.5)<br>② user 発話を episodes に raw 保存 (zero-loss)<br>③ boot 層 dirty 検知 → 再注入 (§8.1)<br>④ recall LLM 起動 → additionalContext 出力 | ⑤ write LLM 起動 (user 発話から fact 抽出) |
| `Stop` | ① 機密チェック (検出時は以下を全部スキップ、§4.5)<br>② assistant 応答を episodes に raw 保存 (zero-loss) | ③ write LLM 起動 (assistant 応答から fact 抽出)<br>④ ③ 完了後に lint LLM を tail 起動 |
| `SessionStart` | ① boot 層 (persona/rule) を全件注入<br>② boot 層の閾値チェック (50 facts / 5000 tokens 超なら `/persona-memory:condense` を提唱、§8.2)<br>③ 前回未処理 episode の再抽出を detach 起動 | (重い初期化があれば detach) |
| `PreCompact` | raw_dump (圧縮前の生データ保全) | session summary を Claude エスカレーションで生成 |
| `SessionEnd` | raw_dump + GC | 大量 lint パス (溜まった矛盾を一括解消) |

### 11.1 設計の軸

1. **raw 保存は完全同期** — UserPromptSubmit と Stop の出口で必ず DB に書き終わっている。回線断 / PC 落ちに耐えるための物理担保
2. **LLM 処理は全部 detach** — write / recall / lint いずれも別プロセス。Ollama を 3 インスタンス並列稼働させる構成と整合
3. **耐障害性**: write 中に落ちても raw は残る → 次回 SessionStart で未処理 episode を検出して再抽出を流す
4. **連投重複**: write LLM が同 fact に対して同時に走っても last-write-wins で自然解消

### 11.2 「全発話」 の定義

user 発話 + assistant 応答の **両方** が write 対象。発火 hook が分かれている (UserPromptSubmit / Stop) ので両側で抽出が走る。

---

## 12. 未決事項

- 並列 LLM のハードウェア上限 (テスト時に判明する想定 — 議論段階では考慮外)
- `CHAIN_DEPTH` (履歴 chain の最大深度、テストで決定)
- boot 層の閾値 (50 facts / 5000 tokens、テストで調整)
- `source` 列の構造化フォーマット (JSON / 区切り / 別列、今後決定)
- `/persona-memory:learn-from <URL>` バッチコマンドを MVP に含めるか
- 重み式の正規化 (各因子のスケール統一、テストで決定)
- lint の詳細ロジック (近傍距離閾値、auto-resolve できる矛盾の種類など)

---

## 13. 外部知識の取り込み

### 13.1 方針: 同じ facts テーブルに同居

記事や論文など外部知識の取り込みも、個人記憶と同じ facts テーブルに入れる。**別 DB / 別テーブルは作らない**。

**理由**:
- 北極星「自然に成長 / 全部繋がって recall する」 と整合
- supersedes / lint / recall ロジックが共通化できる
- 別 DB だと recall 時に毎回 union が必要になる (思想に反する)

### 13.2 source 列の運用

代わりに `facts.source` を構造化された文字列として運用する:

- URL (取り込み元、空の場合は `"conversation"`)
- 取り込み日 (created_at と冗長だが、後で source 単独で見たい時に楽)
- topic タグ (オプション、例: `topic:rust`、`topic:claude-code`)

形式は今後決定 (JSON 文字列 / 区切り文字 / 別列に分ける、など)。recall 時のフィルタ (例: 「rust に関するナレッジだけ引け」) を SQL レベルで可能にする。

### 13.3 取り込み経路

- **default**: 会話中に「この記事読んだ」 と話せば write LLM が抽出 (北極星: 自然な記憶)
- **バッチ**: 必要があればスラッシュコマンド `/persona-memory:learn-from <URL>` を別途用意 (MVP に含めるかは未決)

---

## 14. エスカレーション実装経路

### 14.1 Claude Code 親経由で `claude -p` を呼ぶ

ローカル LLM がエスカレーション条件 (§3.1) を満たした時、**子プロセスとして `claude -p <prompt>` を起動** して Claude に処理させる。Anthropic API 直叩き / SDK は使わない。

**理由**: 認証・課金・プラグイン環境がすでに Claude Code 親に集約されている。子プロセスとして呼び出すのが最も摩擦が少ない。

### 14.2 子プロセスの hook 再帰防止

子プロセスでは環境変数 (例: `PERSONA_ESCALATION_CHILD=1`) を立て、各 hook の冒頭で skip ガードを入れる:

```sh
[ -n "$PERSONA_ESCALATION_CHILD" ] && exit 0
```

これで子側で再度 recall / write / lint が走る無限再帰を防ぐ。現行の `PERSONA_SUMMARY_CHILD` と同じ仕組み。

---

## 15. MVP 機能境界

### 15.1 MVP に入れる (必須)

北極星 (3 原則) と耐障害性に直結する機能のみ。

1. **DB 基盤**: facts / fact_embeddings / episodes / episode_embeddings / escalation_log
2. **raw 保存 (同期)**: UserPromptSubmit と Stop の出口で必ず DB 入り = 「全会話を記憶」 の物理担保
3. **機密フィルタ**: 両 hook 入口チェック + stderr 警告 (override 機構は後)
4. **write LLM**: 全発話 fact 抽出、key+embedding ハイブリッド類似判定、supersedes チェーン、last-write-wins
5. **recall LLM**: LLM インターセプト方式、`BUFFER_N=3`、facts active のみ default
6. **boot 層**: SessionStart 全件注入 + UserPromptSubmit での dirty 再注入 (§8.1)
7. **エスカレーション**: 3 OR 条件 + `claude -p` 子プロセス + 再帰防止ガード
8. **デバッグモード**: `PERSONA_MEMORY_DEBUG`、default レベル c
9. **未処理 episode 再抽出**: SessionStart で前回分を検出して detach 起動
10. **重み付け recall**: `relevance × recency × importance × access_count` (実装時のヒューリスティックで調整)
11. **hook 5 種**: SessionStart / UserPromptSubmit / Stop / PreCompact / SessionEnd

### 15.2 Post-MVP (= v2 以降に回す)

- **lint LLM** (3 インスタンス目): write が安定してから追加。MVP は **write/recall の 2 並列**
- **episodes 検索 (会話引き戻し / 履歴 chain)**: facts active のみで精度を見て、不足を感じたら拡張
- **`/persona-memory:condense`**: boot 層が実際に肥大化してから (閾値警告だけは MVP で出す)
- **`/persona-memory:learn-from <URL>`**: 自然な会話路線で十分か確認してから判断
- **`/persona-memory:allow-last` + `secret-allowlist`**: 警告のみで様子見、誤検出頻発したら追加
- **3 並列 Ollama**: 性能課題が出てから (MVP は 2 並列)
- **`source` 列の構造化フォーマット**: MVP は単純文字列 (URL or `"conversation"`)、topic タグは後

### 15.3 MVP の暗黙ルール

- 矛盾は MVP では蓄積される (lint 無し)。recall 時のノイズが許容範囲か運用で見る
- 機密誤検出は警告でユーザーに通知のみ。誤検出時は手動で対象発話を言い換えて再投稿
- 性能課題 (Ollama 2 並列でメモリ不足等) が出たら都度対策

---

## 16. 実装計画

### 16.1 旧コードの退避

- ブランチ `redesign` を切って完全置き換え
- main は無傷で残し、検証完了後に merge / squash
- plugin cache (`~/.claude/plugins/cache/`) は直接いじらない (memory 既出)

### 16.2 ディレクトリ構成

```
persona-memory/
├── .claude/
│   ├── hooks/              # 新 hook 5 種
│   ├── settings.json
│   └── commands/           # /persona-memory:* (MVP では最小、v2 で拡充)
├── scripts/
│   ├── db/                 # スキーマ・migration・接続
│   ├── write/              # write LLM 経路 (抽出 / supersedes / 類似判定)
│   ├── recall/             # recall LLM 経路 (キーワード抽出 / 検索 / 注入)
│   ├── secrets/            # 機密フィルタ (regex / エントロピー / Keychain ガイダンス)
│   ├── escalate/           # claude -p 呼び出しラッパ
│   ├── debug/              # PERSONA_MEMORY_DEBUG ログ
│   └── shared/             # 共通ユーティリティ (env, ollama client, etc)
├── data/                   # DB ファイル (gitignore 維持)
├── docs/
│   └── redesign-spec.md
├── tests/
├── .mcp.json.template
└── README.md
```

### 16.3 段階的実装の順序

各段階で動作確認を入れながら積む。

1. **DB 基盤** — テーブル作成 + migration
2. **raw 保存 hook** — UserPromptSubmit / Stop の同期処理 + 機密フィルタ (この段階で「全会話 raw 保存」 が機能する)
3. **write LLM 経路** — 抽出 + 類似判定 + supersedes
4. **recall LLM 経路** — キーワード抽出 + 検索 + additionalContext
5. **boot 層注入** — SessionStart + UserPromptSubmit dirty 再注入
6. **エスカレーション** — claude -p 子プロセス + 再帰防止ガード
7. **デバッグモード**
8. **未処理 episode 再抽出** — SessionStart で前回分検出

### 16.4 テスト方針

- **単体**: DB 操作 / 機密フィルタ regex / 類似判定 (key + embedding)
- **統合**: hook → DB → recall の流れを擬似 payload で通す
- **E2E**: `claude` を立ち上げて手動会話、debug ログで recall の中身を確認
- **モック層を最初に作る**: Ollama 呼び出しは mock 化、ローカル LLM 振る舞いを fake で固定 (テスト時間爆発を回避)

---

## 17. 議論で却下した案

- **TTL / 期限切れ削除**: 北極星違反
- **knowledge カテゴリの新設**: 個人情報と外部知識を分離するのは思想に反する。skill / context / preference / rule に同居させる
- **recall = embedding 単発路線**: クエリの理解不足で短発話・指示語に弱い
- **write 並列時の lock / queue / judge**: last-write-wins で十分、複雑度を増やさない
- **旧版 fact の完全上書き (案 B)**: 記憶を捨てるので北極星違反
- **旧版と新版を同居させ recall LLM の時系列判定に任せる (案 C)**: 精度ガチャ + 毎回 LLM 判定で遅い
