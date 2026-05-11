PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- すべての timestamp は Asia/Tokyo (JST = UTC+9) で文字列保存。
-- SQLite は TZ サポートを持たないので datetime('now', '+9 hours') で生成。

CREATE TABLE IF NOT EXISTS facts (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  category          TEXT NOT NULL CHECK (category IN ('persona','rule','preference','aversion','profile','skill','context','knowledge')),
  key               TEXT NOT NULL,
  value             TEXT NOT NULL,
  importance        INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 9),
  access_count      INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL CHECK (status IN ('active','superseded')) DEFAULT 'active',
  supersedes        INTEGER REFERENCES facts(id),
  superseded_by     INTEGER REFERENCES facts(id),
  source            TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  updated_at        TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  last_accessed_at  TEXT
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
  timestamp   TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
);

CREATE INDEX IF NOT EXISTS idx_episodes_session   ON episodes(session_id);
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
