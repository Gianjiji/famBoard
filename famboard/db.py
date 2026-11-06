"""SQLite storage: schema and connection helper.

The Pi-hole is the source of raw DNS events; everything the household owner
curates (people, device assignment, the domain->category index) lives here.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- People the owner registers. kind: 'adult' | 'minor'.
CREATE TABLE IF NOT EXISTS person (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('adult', 'minor')),
    birth_year  INTEGER,
    note        TEXT,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Devices seen on the network, identified by MAC (stable per home network).
-- person_id set  -> device belongs to that person.
-- is_shared = 1   -> "family / shared use" (no single owner).
CREATE TABLE IF NOT EXISTS device (
    id          INTEGER PRIMARY KEY,
    mac         TEXT UNIQUE NOT NULL,
    label       TEXT,
    vendor      TEXT,
    device_type TEXT,
    person_id   INTEGER REFERENCES person(id) ON DELETE SET NULL,
    is_shared   INTEGER NOT NULL DEFAULT 0,
    -- When set, this MAC is an alias merged into another (canonical) device,
    -- e.g. a randomized MAC of a phone already known under a different MAC.
    merged_into INTEGER REFERENCES device(id) ON DELETE SET NULL,
    first_seen  INTEGER,
    last_seen   INTEGER,
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Current IP -> MAC map, refreshed from /api/network/devices. Lets us attach
-- an incoming query (logged by IP) to a device (keyed by MAC).
CREATE TABLE IF NOT EXISTS ip_map (
    ip          TEXT PRIMARY KEY,
    mac         TEXT NOT NULL,
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- Category taxonomy. risk_level: 0 normal, 1 watch, 2 high.
-- minor_sensitive = 1 -> surfaced as a concern for minors.
CREATE TABLE IF NOT EXISTS category (
    key             TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    risk_level      INTEGER NOT NULL DEFAULT 0,
    minor_sensitive INTEGER NOT NULL DEFAULT 0,
    color           TEXT,
    critical        INTEGER NOT NULL DEFAULT 0
);

-- Domain -> category index, built from categorized block/allow lists.
CREATE TABLE IF NOT EXISTS domain_category (
    domain       TEXT PRIMARY KEY,
    category_key TEXT NOT NULL REFERENCES category(key),
    source       TEXT
);

-- Ingested, classified DNS events. id = Pi-hole query id (dedup on conflict).
CREATE TABLE IF NOT EXISTS query_event (
    id           INTEGER PRIMARY KEY,
    ts           REAL NOT NULL,
    client_ip    TEXT,
    mac          TEXT,
    domain       TEXT NOT NULL,
    qtype        TEXT,
    status       TEXT,
    blocked      INTEGER NOT NULL DEFAULT 0,
    category_key TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_ts       ON query_event(ts);
CREATE INDEX IF NOT EXISTS idx_event_mac      ON query_event(mac);
CREATE INDEX IF NOT EXISTS idx_event_category ON query_event(category_key);
CREATE INDEX IF NOT EXISTS idx_event_domain   ON query_event(domain);

-- Alerts: a minor-sensitive category that was actually REACHED (not blocked).
-- id = the triggering query_event id, so re-ingesting dedupes.
CREATE TABLE IF NOT EXISTS alert (
    id           INTEGER PRIMARY KEY,
    ts           REAL NOT NULL,
    mac          TEXT,
    domain       TEXT,
    category_key TEXT,
    risk_level   INTEGER NOT NULL DEFAULT 0,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    notified     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alert_ts  ON alert(ts);
CREATE INDEX IF NOT EXISTS idx_alert_mac ON alert(mac);

-- Internet-access control rules for a device (MAC).
-- target: 'all' (whole device) or a category key (e.g. 'entertainment').
-- mode:   'block'        always blocked while enabled
--         'once'         blocked in the one-off window [from_ts, until_ts]
--                        (from_ts null = now; until_ts null = until turned off)
--         'schedule'     blocked during the recurring windows
--         'allow_window' allowed ONLY during the windows, blocked otherwise
-- windows: JSON list of {"days":[0-6 Mon=0],"start":"HH:MM","end":"HH:MM"}
CREATE TABLE IF NOT EXISTS control_rule (
    id            INTEGER PRIMARY KEY,
    mac           TEXT NOT NULL,
    target        TEXT NOT NULL DEFAULT 'all',
    mode          TEXT NOT NULL,
    from_ts       REAL,
    until_ts      REAL,
    windows       TEXT,
    quota_minutes INTEGER,
    enabled       INTEGER NOT NULL DEFAULT 1,
    note          TEXT,
    created_at    INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_control_mac ON control_rule(mac);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Kids blocklists a minor has opted into. list_key is a catalog key
-- (famboard/kidsblock.py); the lists apply ONLY to that person's devices.
CREATE TABLE IF NOT EXISTS kid_list (
    person_id  INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    list_key   TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (person_id, list_key)
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created (idempotent)."""
    wanted = {
        "category": [("critical", "INTEGER NOT NULL DEFAULT 0")],
        "control_rule": [("quota_minutes", "INTEGER")],
        "device": [("merged_into", "INTEGER REFERENCES device(id)")],
    }
    for table, cols in wanted.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def init_db(db_path: Path | str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
