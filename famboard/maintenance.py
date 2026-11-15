"""Housekeeping for the SQLite file: retention, compaction, planner statistics.

Nothing here is needed for correctness. The database only ever grows (every DNS
query the household makes becomes a row), so these keep it from growing without
bound and from wasting space it already has.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from . import db

log = logging.getLogger("famboard.maintenance")

RETENTION_CHECK_INTERVAL_S = 86400
PRUNE_BATCH = 20000

_COMPACT_DDL = """
CREATE TABLE domain_category_compact (
    domain       TEXT PRIMARY KEY,
    category_key TEXT NOT NULL REFERENCES category(key),
    source       TEXT
) WITHOUT ROWID
"""


def events_older_than(conn: sqlite3.Connection, days: int, now: float | None = None) -> int:
    cutoff = (time.time() if now is None else now) - days * 86400
    return conn.execute(
        "SELECT COUNT(*) FROM query_event INDEXED BY idx_event_ts WHERE ts < ?",
        (cutoff,)).fetchone()[0]


def prune_events(conn: sqlite3.Connection, days: int, now: float | None = None,
                 batch: int = PRUNE_BATCH) -> int:
    """Delete DNS events older than `days` days; return how many went.

    Alerts are a separate table and are kept: what was flagged stays on record
    even after the raw events behind it have aged out. Deletion runs in batches
    with a commit after each, so the web requests sharing the file are never
    locked out for long. days <= 0 means "keep everything" and deletes nothing.
    """
    if days <= 0:
        return 0
    cutoff = (time.time() if now is None else now) - days * 86400
    removed = 0
    while True:
        cur = conn.execute(
            "DELETE FROM query_event WHERE id IN ("
            "  SELECT id FROM query_event INDEXED BY idx_event_ts WHERE ts < ? LIMIT ?)",
            (cutoff, batch))
        conn.commit()
        removed += cur.rowcount
        if cur.rowcount < batch:
            return removed


def run_retention(conn: sqlite3.Connection, days: int, now: float | None = None) -> int | None:
    """Apply the retention setting at most once a day. Called from the poller
    on every cycle; returns the rows removed, or None when it was not due."""
    if days <= 0:
        return None
    now = time.time() if now is None else now
    last = float(db.get_meta(conn, "retention_checked_ts", "0") or 0)
    if now - last < RETENTION_CHECK_INTERVAL_S:
        return None
    db.set_meta(conn, "retention_checked_ts", str(now))
    conn.commit()
    removed = prune_events(conn, days, now=now)
    if removed:
        log.info("retention: rimossi %d eventi più vecchi di %d giorni", removed, days)
    return removed


def domain_category_is_compact(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='domain_category'").fetchone()
    return bool(row and row[0] and "WITHOUT ROWID" in row[0].upper())


def compact_domain_category(conn: sqlite3.Connection) -> bool:
    """Rebuild domain_category as a WITHOUT ROWID table; False if it already is.

    The domain->category index is millions of rows keyed by a text domain. As
    an ordinary table SQLite stores every domain twice: once in the table and
    once in the automatic index that enforces the primary key. WITHOUT ROWID
    keeps a single b-tree keyed by the domain, roughly halving the space, and
    lookups read one tree instead of two. Contents and queries are unchanged.
    The copy happens inside one transaction: it either completes or leaves the
    old table untouched.
    """
    if domain_category_is_compact(conn):
        return False
    conn.execute("DROP TABLE IF EXISTS domain_category_compact")
    conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_COMPACT_DDL)
        conn.execute(
            "INSERT INTO domain_category_compact(domain, category_key, source) "
            "SELECT domain, category_key, source FROM domain_category ORDER BY domain")
        conn.execute("DROP TABLE domain_category")
        conn.execute("ALTER TABLE domain_category_compact RENAME TO domain_category")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return True


def optimize(conn: sqlite3.Connection, vacuum: bool = True) -> dict:
    """Compact the domain index, refresh planner statistics and, with `vacuum`,
    hand the freed pages back to the filesystem.

    VACUUM rewrites the whole file: it needs about as much free disk as the
    database is large and exclusive access, so run it with the service stopped.
    """
    def size() -> int:
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        return pages * conn.execute("PRAGMA page_size").fetchone()[0]

    before = size()
    compacted = compact_domain_category(conn)
    conn.execute("PRAGMA analysis_limit = 1000")
    conn.execute("ANALYZE")
    conn.commit()
    if vacuum:
        conn.execute("VACUUM")
    return {"compacted": compacted, "vacuumed": vacuum,
            "bytes_before": before, "bytes_after": size()}
