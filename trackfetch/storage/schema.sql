-- Trackfetch local store.
-- Every statement is idempotent so opening an existing database is safe.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_info (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'pasted',
    created_at REAL NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS batch_items (
    id         TEXT PRIMARY KEY,
    batch_id   TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL DEFAULT 0,
    raw_text   TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'QUERY',
    extra_json TEXT NOT NULL DEFAULT '{}',
    source     TEXT NOT NULL DEFAULT 'pasted',
    skipped    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items(batch_id, position);

CREATE TABLE IF NOT EXISTS queries (
    id            TEXT PRIMARY KEY,
    batch_item_id TEXT NOT NULL REFERENCES batch_items(id) ON DELETE CASCADE,
    provider_id   TEXT NOT NULL,
    text          TEXT NOT NULL DEFAULT '',
    remote_id     TEXT,
    state         TEXT NOT NULL DEFAULT 'pending',
    error         TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    completed_at  REAL,
    result_count  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queries_item ON queries(batch_item_id);

CREATE TABLE IF NOT EXISTS candidates (
    id             TEXT PRIMARY KEY,
    query_id       TEXT NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    provider_id    TEXT NOT NULL,
    filename       TEXT NOT NULL DEFAULT '',
    directory      TEXT NOT NULL DEFAULT '',
    extension      TEXT NOT NULL DEFAULT '',
    size           INTEGER,
    bitrate        INTEGER,
    is_vbr         INTEGER,
    duration       INTEGER,
    sample_rate    INTEGER,
    bit_depth      INTEGER,
    codec          TEXT NOT NULL DEFAULT '',
    remote_user    TEXT NOT NULL DEFAULT '',
    queue_length   INTEGER,
    has_free_slot  INTEGER,
    upload_speed   INTEGER,
    locked         INTEGER NOT NULL DEFAULT 0,
    relevance      REAL NOT NULL DEFAULT 0,
    selected       INTEGER NOT NULL DEFAULT 0,
    payload_json   TEXT NOT NULL DEFAULT '{}',
    raw_json       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_candidates_query ON candidates(query_id);
CREATE INDEX IF NOT EXISTS idx_candidates_selected ON candidates(selected);

CREATE TABLE IF NOT EXISTS transfers (
    id               TEXT PRIMARY KEY,
    provider_id      TEXT NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'Waiting',
    total_bytes      INTEGER,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    speed_bps        REAL,
    eta_seconds      INTEGER,
    destination      TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    remedy           TEXT NOT NULL DEFAULT '',
    remote_id        TEXT NOT NULL DEFAULT '',
    payload_json     TEXT NOT NULL DEFAULT '{}',
    batch_item_id    TEXT NOT NULL DEFAULT '',
    candidate_id     TEXT NOT NULL DEFAULT '',
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    completed_at     REAL,
    attempts         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_transfers_status ON transfers(status);
CREATE INDEX IF NOT EXISTS idx_transfers_provider ON transfers(provider_id);

-- Record of what has actually finished, so a repeat run does not fetch the
-- same file again unless the user explicitly asks for it.
CREATE TABLE IF NOT EXISTS completions (
    fingerprint  TEXT PRIMARY KEY,
    provider_id  TEXT NOT NULL DEFAULT '',
    label        TEXT NOT NULL DEFAULT '',
    destination  TEXT NOT NULL DEFAULT '',
    size         INTEGER,
    completed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Short rolling record of notable events, used by Copy Diagnostic Information.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    level       TEXT NOT NULL DEFAULT 'info',
    provider_id TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
