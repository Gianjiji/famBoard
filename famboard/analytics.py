"""Read-side queries for the dashboard."""
from __future__ import annotations

import sqlite3
import time

WINDOWS = {"24h": 86400, "7d": 604800, "30d": 2592000}


def since_ts(window: str) -> float:
    return time.time() - WINDOWS.get(window, 86400)


def _events(macs: list[str] | None = None, cat_key: str | None = None) -> str:
    """FROM item for a time-window read of query_event.

    Every dashboard query looks at "the last N days". When nothing else narrows
    the rows (no device, no category) the only sensible way in is the index on
    ts, and the planner must not be left to choose: for a GROUP BY domain (or
    mac) it prefers to walk the whole table in the order of that column's index
    to save a sort, reading every event ever stored to keep the few thousand in
    the window. On two million events that made the home page's top domains
    take over two seconds, against a few hundredths through idx_event_ts.
    Statistics do not help: the bound of the window is a parameter the planner
    cannot see, and after an ANALYZE it mis-picked a different query instead.

    With a device or category filter the planner is left free: those indexes
    are selective and it uses them correctly.
    """
    if macs or cat_key:
        return "query_event e"
    return "query_event e INDEXED BY idx_event_ts"


def overview(conn: sqlite3.Connection, since: float) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) total, "
        "       COALESCE(SUM(blocked),0) blocked, "
        "       COALESCE(SUM(CASE WHEN category_key IS NOT NULL THEN 1 ELSE 0 END),0) classified "
        f"FROM {_events()} WHERE ts >= ?", (since,)).fetchone()
    return dict(total=row["total"], blocked=row["blocked"], classified=row["classified"])


def category_breakdown(conn: sqlite3.Connection, since: float,
                       macs: list[str] | None = None) -> list[dict]:
    sql = ("SELECT k.key, k.label, k.color, k.risk_level, k.minor_sensitive, COUNT(*) n "
           f"FROM {_events(macs)} JOIN category k ON k.key = e.category_key "
           "WHERE e.ts >= ?")
    params: list = [since]
    if macs is not None:
        if not macs:
            return []
        sql += f" AND e.mac IN ({','.join('?' * len(macs))})"
        params += macs
    sql += " GROUP BY k.key ORDER BY n DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def minor_alerts(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Sensitive-category hits attributed to registered minors."""
    sql = ("SELECT p.id person_id, p.name person, k.label category, k.key cat_key, "
           "       k.color, k.risk_level, COUNT(*) n, MAX(e.ts) last_ts "
           "FROM query_event e "
           "JOIN device d ON d.mac = e.mac "
           "JOIN person p ON p.id = d.person_id "
           "JOIN category k ON k.key = e.category_key "
           "WHERE e.ts >= ? AND p.kind = 'minor' AND k.minor_sensitive = 1 "
           "GROUP BY p.id, k.key ORDER BY k.risk_level DESC, n DESC")
    return [dict(r) for r in conn.execute(sql, (since,)).fetchall()]


def critical_alerts(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Reached events in CRITICAL categories (distress), for ANYONE, incl. unassigned."""
    sql = ("SELECT p.id person_id, COALESCE(p.name, 'dispositivo non assegnato') person, "
           "       p.kind person_kind, COALESCE(d.label, e.mac) device, "
           "       COALESCE(d.merged_into, d.id) device_id, "
           "       k.label category, k.key cat_key, k.color, COUNT(*) n, MAX(e.ts) last_ts "
           f"FROM {_events()} "
           "JOIN category k ON k.key = e.category_key AND k.critical = 1 "
           "LEFT JOIN device d ON d.mac = e.mac "
           "LEFT JOIN person p ON p.id = d.person_id "
           "WHERE e.ts >= ? AND e.blocked = 0 "
           "GROUP BY p.id, k.key, e.mac ORDER BY last_ts DESC")
    return [dict(r) for r in conn.execute(sql, (since,)).fetchall()]


def unassigned_sensitive(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Sensitive hits from devices not yet assigned to a person."""
    sql = ("SELECT COALESCE(d.label, e.mac) device, e.mac, "
           "       COALESCE(d.merged_into, d.id) device_id, "
           "       k.label category, k.key cat_key, k.color, k.risk_level, COUNT(*) n "
           f"FROM {_events()} "
           "LEFT JOIN device d ON d.mac = e.mac "
           "JOIN category k ON k.key = e.category_key "
           "WHERE e.ts >= ? AND k.minor_sensitive = 1 "
           "  AND (d.person_id IS NULL AND COALESCE(d.is_shared,0) = 0) "
           "GROUP BY e.mac, k.key ORDER BY k.risk_level DESC, n DESC")
    return [dict(r) for r in conn.execute(sql, (since,)).fetchall()]


def people(conn: sqlite3.Connection) -> list[dict]:
    sql = ("SELECT p.*, COUNT(d.id) n_devices "
           "FROM person p LEFT JOIN device d ON d.person_id = p.id "
           "GROUP BY p.id ORDER BY p.kind DESC, p.name")
    return [dict(r) for r in conn.execute(sql).fetchall()]


def devices(conn: sqlite3.Connection) -> list[dict]:
    """Canonical devices only (aliases are grouped under them), with MAC count."""
    sql = ("SELECT d.*, p.name person_name, p.kind person_kind, "
           "  1 + (SELECT COUNT(*) FROM device m WHERE m.merged_into = d.id) n_macs "
           "FROM device d LEFT JOIN person p ON p.id = d.person_id "
           "WHERE d.merged_into IS NULL "
           "ORDER BY d.last_seen DESC")
    return [dict(r) for r in conn.execute(sql).fetchall()]


def device_macs_for_person(conn: sqlite3.Connection, person_id: int) -> list[str]:
    """All MACs owned by a person, across every alias of their devices."""
    return [r["mac"] for r in conn.execute(
        "SELECT mac FROM device WHERE person_id = ? "
        "OR merged_into IN (SELECT id FROM device WHERE person_id = ?)",
        (person_id, person_id)).fetchall()]


def _mac_clause(macs: list[str] | None, params: list) -> str:
    if macs is None:
        return ""
    if not macs:
        return " AND 1=0"
    params += macs
    return f" AND e.mac IN ({','.join('?' * len(macs))})"


def activity_timeline(conn: sqlite3.Connection, since: float, bucket_s: int,
                      macs: list[str] | None = None) -> list[dict]:
    params: list = [bucket_s, bucket_s, since]
    sql = ("SELECT CAST(e.ts / ? AS INT) * ? AS bucket, COUNT(*) total, "
           f"COALESCE(SUM(e.blocked),0) blocked FROM {_events(macs)} WHERE e.ts >= ?")
    sql += _mac_clause(macs, params)
    sql += " GROUP BY bucket ORDER BY bucket"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def hourly_distribution(conn: sqlite3.Connection, since: float,
                        macs: list[str] | None = None,
                        sensitive_only: bool = False) -> list[int]:
    params: list = [since]
    sql = ("SELECT CAST(strftime('%H', e.ts, 'unixepoch', 'localtime') AS INT) h, "
           f"COUNT(*) n FROM {_events(macs)}")
    if sensitive_only:
        sql += " JOIN category k ON k.key = e.category_key AND k.minor_sensitive = 1"
    sql += " WHERE e.ts >= ?"
    sql += _mac_clause(macs, params)
    sql += " GROUP BY h"
    buckets = [0] * 24
    for r in conn.execute(sql, params).fetchall():
        if r["h"] is not None:
            buckets[r["h"]] = r["n"]
    return buckets


def top_domains(conn: sqlite3.Connection, since: float, macs: list[str] | None = None,
                cat_key: str | None = None, limit: int = 15) -> list[dict]:
    params: list = [since]
    sql = ("SELECT e.domain, COALESCE(k.label,'—') category, k.color, COUNT(*) n "
           f"FROM {_events(macs, cat_key)} LEFT JOIN category k ON k.key = e.category_key "
           "WHERE e.ts >= ?")
    if cat_key:
        sql += " AND e.category_key = ?"
        params.append(cat_key)
    sql += _mac_clause(macs, params)
    sql += " GROUP BY e.domain ORDER BY n DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def open_alert_count(conn: sqlite3.Connection) -> int:
    """Unacknowledged alerts that concern a minor OR any critical category."""
    row = conn.execute(
        "SELECT COUNT(*) n FROM alert a "
        "LEFT JOIN device d ON d.mac = a.mac "
        "LEFT JOIN person p ON p.id = d.person_id "
        "LEFT JOIN category k ON k.key = a.category_key "
        "WHERE a.acknowledged = 0 AND (p.kind = 'minor' OR k.critical = 1)").fetchone()
    return row["n"]


def coverage(conn: sqlite3.Connection) -> dict:
    idx = conn.execute("SELECT COUNT(*) n FROM domain_category").fetchone()["n"]
    ev = conn.execute("SELECT COUNT(*) n FROM query_event").fetchone()["n"]
    cl = conn.execute("SELECT COUNT(*) n FROM query_event WHERE category_key IS NOT NULL").fetchone()["n"]
    last = conn.execute("SELECT value FROM meta WHERE key='last_ts'").fetchone()
    return {"index": idx, "events": ev, "classified": cl,
            "last_ts": float(last["value"]) if last else None}


def recent_events(conn: sqlite3.Connection, since: float, macs: list[str] | None = None,
                  cat_key: str | None = None, limit: int = 100) -> list[dict]:
    """Visit log (domain + when): reverse-chron, bursts of the same domain
    grouped in 5-minute buckets (DNS repeats a lot)."""
    params: list = [since]
    sql = ("SELECT MAX(e.ts) ts, e.domain, e.blocked, "
           "       k.label category, k.color, COUNT(*) n "
           f"FROM {_events(macs, cat_key)} LEFT JOIN category k ON k.key = e.category_key "
           "WHERE e.ts >= ?")
    if cat_key:
        sql += " AND e.category_key = ?"
        params.append(cat_key)
    sql += _mac_clause(macs, params)
    sql += (" GROUP BY e.domain, e.blocked, CAST(e.ts / 300 AS INT) "
            "ORDER BY ts DESC LIMIT ?")
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def category_log(conn: sqlite3.Connection, since: float, cat_key: str,
                 macs: list[str] | None = None, limit: int = 150) -> list[dict]:
    """Cross-device visit log for one category: which device reached which
    domain and when. Bursts of the same domain from the same device are
    grouped in 5-minute buckets, like recent_events."""
    sql = ("SELECT MAX(e.ts) ts, e.domain, e.blocked, COUNT(*) n, "
           "       COALESCE(d.label, e.mac, '?') device, "
           "       COALESCE(d.merged_into, d.id) device_id "
           "FROM query_event e LEFT JOIN device d ON d.mac = e.mac "
           "WHERE e.ts >= ? AND e.category_key = ?")
    params: list = [since, cat_key]
    sql += _mac_clause(macs, params)
    sql += (" GROUP BY e.domain, e.mac, e.blocked, CAST(e.ts / 300 AS INT) "
            "ORDER BY ts DESC LIMIT ?")
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def recent_by_category(conn: sqlite3.Connection, since: float, cat_key: str,
                      macs: list[str] | None = None, limit: int = 50) -> list[dict]:
    sql = ("SELECT e.ts, e.domain, e.mac, COALESCE(d.label, e.mac) device "
           "FROM query_event e LEFT JOIN device d ON d.mac = e.mac "
           "WHERE e.ts >= ? AND e.category_key = ?")
    params: list = [since, cat_key]
    if macs is not None:
        if not macs:
            return []
        sql += f" AND e.mac IN ({','.join('?' * len(macs))})"
        params += macs
    sql += " ORDER BY e.ts DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
