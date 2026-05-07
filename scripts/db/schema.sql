PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- すべての timestamp は Asia/Tokyo (JST = UTC+9) で文字列保存。
-- SQLite は TZ サポートを持たないので datetime('now', '+9 hours') で生成。

CREATE TABLE IF NOT EXISTS facts (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  category          TEXT NOT NULL CHECK (category IN ('persona','rule','preference','aversion','profile','skill','context')),
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

CREATE TABLE IF NOT EXISTS escalation_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp   TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  reason      TEXT NOT NULL CHECK (reason IN ('long_input','uncertainty','high_importance')),
  input_size  INTEGER,
  caller      TEXT NOT NULL CHECK (caller IN ('write','recall','lint')),
  outcome     TEXT
);

CREATE INDEX IF NOT EXISTS idx_escalation_timestamp ON escalation_log(timestamp);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
