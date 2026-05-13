PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- すべての timestamp は Asia/Tokyo (JST = UTC+9) で文字列保存。
-- SQLite は TZ サポートを持たないので datetime('now', '+9 hours') で生成。

CREATE TABLE IF NOT EXISTS facts (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  category           TEXT NOT NULL CHECK (category IN ('persona','rule','preference','aversion','profile','skill','context','knowledge')),
  key                TEXT NOT NULL,
  value              TEXT NOT NULL,
  importance         INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 9),
  access_count       INTEGER NOT NULL DEFAULT 0,
  status             TEXT NOT NULL CHECK (status IN ('active','superseded')) DEFAULT 'active',
  supersedes         INTEGER REFERENCES facts(id),
  superseded_by      INTEGER REFERENCES facts(id),
  source             TEXT,
  -- 0.6.14 反事実記憶: 撤回理由を保存. recall で「過去には X と言ったが
  -- (理由: Y) のため撤回済」 と参照可能にする。 NULL = 理由不明 (旧データ互換).
  reason_superseded  TEXT,
  created_at         TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  updated_at         TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  last_accessed_at   TEXT
);

-- active 層は (category, key) で一意 (last-write-wins を物理担保)
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_active_unique
  ON facts(category, key) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_facts_category   ON facts(category);
CREATE INDEX IF NOT EXISTS idx_facts_status     ON facts(status);
CREATE INDEX IF NOT EXISTS idx_facts_supersedes ON facts(supersedes);

CREATE TABLE IF NOT EXISTS episodes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  role        TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content     TEXT NOT NULL,
  summary     TEXT,
  session_id  TEXT NOT NULL,
  -- 0.6.24 トピック記憶: 発話を topic に紐付け. SessionStart で発行された
  -- topic_id (= 既定は session_id, continue_topic で既存 ID 継承可) を埋める.
  -- 旧データ (列追加前) は NULL のまま.
  topic_id    TEXT,
  timestamp   TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
);

CREATE INDEX IF NOT EXISTS idx_episodes_session   ON episodes(session_id);
CREATE INDEX IF NOT EXISTS idx_episodes_topic     ON episodes(topic_id);
CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp);

-- ── FTS5 全文検索 (0.6.0 phase3): vec0 cosine の単一依存を解消する ──
-- nomic-embed-text の弁別力限界 (短文 / 固有名詞 / typo に弱い) を BM25 で補う.
-- 日本語のトークナイズは trigram (SQLite 3.34+ 標準) で N-gram 化 = 単語境界
-- 不要で日本語の連続文に効く. external content table 方式で episodes 本体と
-- 同期 (= 二重保存にならない).
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
  content,
  content='episodes',
  content_rowid='id',
  tokenize='trigram'
);

-- episodes <-> episodes_fts の同期 trigger
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
  INSERT INTO episodes_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
  INSERT INTO episodes_fts(episodes_fts, rowid, content) VALUES('delete', old.id, old.content);
  INSERT INTO episodes_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS escalation_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp   TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  reason      TEXT NOT NULL CHECK (reason IN ('long_input','uncertainty','high_importance')),
  input_size  INTEGER,
  caller      TEXT NOT NULL CHECK (caller IN ('write','recall','lint')),
  outcome     TEXT
);

CREATE INDEX IF NOT EXISTS idx_escalation_timestamp ON escalation_log(timestamp);

-- lint LLM (judge_conflict) 検出結果。
-- resolution:
--   'auto_superseded' = confidence >= 90、古い方を facts.status='superseded' に降格 + source='lint_conflict'
--   'flagged'         = confidence 60-89、両方 active のまま記録のみ (recall に影響なし)
-- recall 側は本テーブルを直接見ない。supersede された fact の source 列で
-- 「conflict 由来か」 を判別 → active な側を引いた時に旧版を補足表示する。
CREATE TABLE IF NOT EXISTS conflicts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  fact_a_id    INTEGER NOT NULL REFERENCES facts(id),
  fact_b_id    INTEGER NOT NULL REFERENCES facts(id),
  confidence   INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100),
  resolution   TEXT NOT NULL CHECK (resolution IN ('auto_superseded','flagged')),
  detected_at  TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
);

CREATE INDEX IF NOT EXISTS idx_conflicts_fact_a ON conflicts(fact_a_id);
CREATE INDEX IF NOT EXISTS idx_conflicts_fact_b ON conflicts(fact_b_id);

-- lint LLM 1 回の実行サマリ。健康診断 (/persona-memory:health) で「最近 lint
-- 回ったか」 「auto_resolve / flag の件数」 を見るために使う。
CREATE TABLE IF NOT EXISTS lint_log (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  run_at                   TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  pairs_examined           INTEGER NOT NULL DEFAULT 0,
  conflicts_flagged        INTEGER NOT NULL DEFAULT 0,
  conflicts_auto_resolved  INTEGER NOT NULL DEFAULT 0,
  trigger_kind             TEXT NOT NULL CHECK (trigger_kind IN ('write_tail','session_end','manual'))
);

CREATE INDEX IF NOT EXISTS idx_lint_log_run_at ON lint_log(run_at);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ── 0.6.12 想起トリガー学習 (Recall Trigger Learning) ─────────────────────
-- ユーザーが「前に話した X」「あの〜」 等の再参照表現を投げた瞬間の
-- (query, hit fact_ids/episode_ids) ペアを正例として記録する。
-- Phase B でこの履歴を使い、 類似 query が来た時に過去 hit を boost する
-- パーソナライズ検索を実現する (agentmemory の RRF / 信頼度スコアの代替)。
-- 「忘れない」 原則と完全両立: 履歴は消さず、 重み付けに使うだけ。
CREATE TABLE IF NOT EXISTS recall_triggers (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  source_episode_id INTEGER REFERENCES episodes(id),
  trigger_phrase    TEXT NOT NULL,                  -- 「Phase 1 の決定」 等の抽出 phrase
  query_embedding   BLOB,                            -- phrase ではなく user 発話全文の embedding
  hit_fact_ids      TEXT NOT NULL DEFAULT '[]',     -- JSON array of fact ids
  hit_episode_ids   TEXT NOT NULL DEFAULT '[]'      -- JSON array of episode ids
);

CREATE INDEX IF NOT EXISTS idx_recall_triggers_ts        ON recall_triggers(ts);
CREATE INDEX IF NOT EXISTS idx_recall_triggers_source_ep ON recall_triggers(source_episode_id);

-- ── 0.6.13 議論グラフ (Discussion Graph) Phase A1 ────────────────────────
-- agentmemory の静的知識グラフを「時系列・状態遷移を持つ DAG」 に進化させた
-- 構造ナビゲーション基盤。 「あの議論はどう決まった?」 系の query で
-- ランキング検索ではなく "末端 accepted ノード即答" を実現する素材。
-- 「忘れない」 原則: rejected / superseded ノードも削除せず状態のみ遷移。
--
-- kind 例: 'topic' (論点) / 'option' (検討案) / 'decision' (採用判断) /
--          'retraction' (撤回) / 'rationale' (理由)
-- state 例: 'proposed' / 'accepted' / 'rejected' / 'superseded' / 'observed'
CREATE TABLE IF NOT EXISTS discussion_nodes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  episode_id  INTEGER REFERENCES episodes(id),  -- 抽出元発話 (NULL 可)
  kind        TEXT NOT NULL,
  title       TEXT NOT NULL,                     -- 短い見出し (検索用)
  state       TEXT NOT NULL DEFAULT 'proposed',
  content     TEXT,                              -- 自由記述 (任意)
  embedding   BLOB                               -- title + content の embedding (Phase B 用)
);

CREATE INDEX IF NOT EXISTS idx_discussion_nodes_ts      ON discussion_nodes(ts);
CREATE INDEX IF NOT EXISTS idx_discussion_nodes_state   ON discussion_nodes(state);
CREATE INDEX IF NOT EXISTS idx_discussion_nodes_kind    ON discussion_nodes(kind);
CREATE INDEX IF NOT EXISTS idx_discussion_nodes_episode ON discussion_nodes(episode_id);

-- edge_kind 例: 'derives_from' (派生) / 'considers' (論点が検討案を含む) /
--               'decides' (検討案を採用) / 'retracts' (撤回) /
--               'supersedes' (上書き) / 'depends_on' (前提)
CREATE TABLE IF NOT EXISTS discussion_edges (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  src_id     INTEGER NOT NULL REFERENCES discussion_nodes(id),
  dst_id     INTEGER NOT NULL REFERENCES discussion_nodes(id),
  edge_kind  TEXT NOT NULL,
  ts         TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
);

CREATE INDEX IF NOT EXISTS idx_discussion_edges_src  ON discussion_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_discussion_edges_dst  ON discussion_edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_discussion_edges_kind ON discussion_edges(edge_kind);

-- ── 0.6.24 トピック記憶 (Topic Memory) ────────────────────────────────────
-- 「話題ID + タグ」 の 2 階層で議論を構造化. 案 1 (discussion_nodes) は
-- 単発判定で 0.5% しか抽出できなかったため、 セッション基盤の topic_id +
-- 軽量 tag 抽出に置き換える設計.
--
-- topics: 1 セッション = 1 topic 基本. main agent が「前回の続き」 と判断
--   したら continue_topic で既存 ID 継承.
-- topic_tags: write LLM が turn 単位で抽出した短い名詞句. recall で発話
--   embed と近傍検索 → topic_id 集計.
-- topic_relations: Phase 4 で「派生 / 合流 / 撤回」 を辿るための DAG.
-- episodes.topic_id: SessionStart で発行された ID を全 turn に紐付け.
--
-- 既存 facts/episodes vec 検索路 (casual recall) は無傷で並列維持.
CREATE TABLE IF NOT EXISTS topics (
  id              TEXT PRIMARY KEY,
  title           TEXT,
  summary         TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  last_active_at  TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
);

CREATE INDEX IF NOT EXISTS idx_topics_last_active ON topics(last_active_at);

CREATE TABLE IF NOT EXISTS topic_tags (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id    TEXT NOT NULL REFERENCES topics(id),
  tag         TEXT NOT NULL,
  ts          TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
);

CREATE INDEX IF NOT EXISTS idx_topic_tags_topic_id ON topic_tags(topic_id);
CREATE INDEX IF NOT EXISTS idx_topic_tags_tag      ON topic_tags(tag);

CREATE TABLE IF NOT EXISTS topic_relations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  from_topic_id  TEXT NOT NULL REFERENCES topics(id),
  to_topic_id    TEXT NOT NULL REFERENCES topics(id),
  kind           TEXT NOT NULL,
  ts             TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
);

CREATE INDEX IF NOT EXISTS idx_topic_relations_from ON topic_relations(from_topic_id);
CREATE INDEX IF NOT EXISTS idx_topic_relations_to   ON topic_relations(to_topic_id);
