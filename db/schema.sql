PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- すべての timestamp は Asia/Tokyo (JST = UTC+9) で保存する。
-- SQLite はネイティブな TZ サポートを持たないので、`datetime('now', '+9 hours')`
-- で UTC + 9h の文字列を生成して格納する。日本語ユーザーが DB を直接覗いた時に
-- 時差なしで読めることを優先。Python 側の age 計算もこの規約に合わせる。

CREATE TABLE IF NOT EXISTS facts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  category    TEXT NOT NULL,
  key         TEXT NOT NULL,
  value       TEXT NOT NULL,
  importance  INTEGER NOT NULL DEFAULT 5,
  status      TEXT NOT NULL DEFAULT 'active',
  source      TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  UNIQUE(category, key)
);

CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_facts_status   ON facts(status);

CREATE TABLE IF NOT EXISTS episodes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL,
  summary     TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
);

CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id);
CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at);

CREATE TABLE IF NOT EXISTS lint_log (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  run_at              TEXT NOT NULL DEFAULT (datetime('now', '+9 hours')),
  pairs_examined      INTEGER NOT NULL DEFAULT 0,
  conflicts_flagged   INTEGER NOT NULL DEFAULT 0,
  conflicts_auto_resolved INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conflicts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  fact_a_id   INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  fact_b_id   INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
  confidence  INTEGER NOT NULL,
  resolution  TEXT NOT NULL DEFAULT 'pending',
  detected_at TEXT NOT NULL DEFAULT (datetime('now', '+9 hours'))
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
