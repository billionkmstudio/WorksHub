CREATE TABLE IF NOT EXISTS items (
  id        TEXT PRIMARY KEY,
  url       TEXT UNIQUE,
  source    TEXT NOT NULL DEFAULT 'web',
  title     TEXT NOT NULL DEFAULT '',
  summary   TEXT NOT NULL DEFAULT '',
  text      TEXT NOT NULL DEFAULT '',
  note      TEXT NOT NULL DEFAULT '',
  category  TEXT NOT NULL DEFAULT '未分類',
  tags      TEXT NOT NULL DEFAULT '[]',
  ai        INTEGER NOT NULL DEFAULT 0,
  saved_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_saved ON items (saved_at DESC);
