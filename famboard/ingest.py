"""Pull data from the Pi-hole into the local DB, classify it, raise alerts.

One cycle: refresh the device inventory + IP->MAC map, pull new query events,
attach each to a device (via IP->MAC) and a category (via the domain->category
index), store them, raise alerts for minor-sensitive categories that were
actually reached, and fire notifications for alerts on minors.
"""
from __future__ import annotations

import sqlite3
import time

from . import db, notify
from .config import settings
from .pihole import PiholeClient

BLOCKED_STATUSES = {
    "GRAVITY", "DENYLIST", "REGEX", "EXTERNAL_BLOCKED_IP",
    "EXTERNAL_BLOCKED_NULL", "EXTERNAL_BLOCKED_NXRA", "GRAVITY_CNAME",
    "REGEX_CNAME", "DENYLIST_CNAME", "SPECIAL_DOMAIN",
}

# Rough device-type guess from the MAC vendor string.
_VENDOR_TYPE = [
    ("apple", "apple"), ("samsung", "phone"), ("samjin", "phone"),
    ("xiaomi", "phone"), ("huawei", "phone"), ("oneplus", "phone"),
    ("google", "phone"), ("motorola", "phone"), ("oppo", "phone"),
    ("amazon", "iot"), ("espressif", "iot"), ("sonos", "iot"),
    ("tuya", "iot"), ("shelly", "iot"), ("raspberry", "computer"),
    ("intel", "computer"), ("dell", "computer"), ("asus", "computer"),
    ("lenovo", "computer"), ("microsoft", "computer"), ("tp-link", "network"),
    ("proxmox", "server"), ("nintendo", "console"), ("sony", "console"),
    ("lg electronics", "tv"), ("hisense", "tv"), ("roku", "tv"),
]


def guess_device_type(vendor: str | None) -> str | None:
    if not vendor:
        return None
    v = vendor.lower()
    for needle, dtype in _VENDOR_TYPE:
        if needle in v:
            return dtype
    return None


def _category_meta(conn: sqlite3.Connection) -> dict[str, tuple[int, int, int]]:
    return {r["key"]: (r["risk_level"], r["minor_sensitive"], r["critical"])
            for r in conn.execute(
                "SELECT key, risk_level, minor_sensitive, critical FROM category").fetchall()}


def refresh_devices(conn: sqlite3.Connection, client: PiholeClient) -> int:
    devices = client.network_devices()
    now = int(time.time())
    for d in devices:
        mac = (d.get("hwaddr") or "").lower()
        if not mac or ":" not in mac:
            continue
        vendor = d.get("macVendor") or None
        conn.execute(
            "INSERT INTO device(mac, vendor, device_type, first_seen, last_seen) "
            "VALUES(?,?,?,?,?) ON CONFLICT(mac) DO UPDATE SET "
            "vendor=excluded.vendor, last_seen=excluded.last_seen",
            (mac, vendor, guess_device_type(vendor), d.get("firstSeen"), d.get("lastQuery")))
        # backfill a type guess for rows that don't have one yet
        conn.execute(
            "UPDATE device SET device_type=? WHERE mac=? AND device_type IS NULL",
            (guess_device_type(vendor), mac))
        for addr in d.get("ips", []):
            ip = addr.get("ip")
            if ip:
                conn.execute(
                    "INSERT INTO ip_map(ip, mac, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(ip) DO UPDATE SET mac=excluded.mac, updated_at=excluded.updated_at",
                    (ip, mac, now))
    conn.commit()
    return len(devices)


def classify_domain(conn: sqlite3.Connection, domain: str,
                    cache: dict[str, str | None]) -> str | None:
    domain = (domain or "").lower().rstrip(".")
    if domain in cache:
        return cache[domain]
    parts = domain.split(".")
    result: str | None = None
    for i in range(len(parts) - 1):
        row = conn.execute("SELECT category_key FROM domain_category WHERE domain = ?",
                           (".".join(parts[i:]),)).fetchone()
        if row:
            result = row["category_key"]
            break
    cache[domain] = result
    return result


def _ingest_window(conn: sqlite3.Connection, client: PiholeClient, since: float,
                  until: float | None, cat_meta: dict, cache: dict,
                  ip_to_mac: dict, make_alerts: bool, alerts_notified: int) -> tuple[int, float]:
    inserted = 0
    max_ts = since
    for q in client.queries(since_ts=since, until_ts=until):
        qid = q.get("id")
        domain = q.get("domain")
        if qid is None or not domain:
            continue
        ts = float(q.get("time", 0.0))
        status = q.get("status")
        client_ip = (q.get("client") or {}).get("ip")
        mac = ip_to_mac.get(client_ip)
        blocked = 1 if status in BLOCKED_STATUSES else 0
        category = classify_domain(conn, domain, cache)
        cur = conn.execute(
            "INSERT OR IGNORE INTO query_event"
            "(id, ts, client_ip, mac, domain, qtype, status, blocked, category_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (qid, ts, client_ip, mac, domain, q.get("type"), status, blocked, category))
        if cur.rowcount:
            inserted += 1
        if category and not blocked and make_alerts:
            risk, minor_sensitive, critical = cat_meta.get(category, (0, 0, 0))
            if minor_sensitive or critical:
                conn.execute(
                    "INSERT OR IGNORE INTO alert"
                    "(id, ts, mac, domain, category_key, risk_level, notified) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (qid, ts, mac, domain, category, risk, alerts_notified))
        if ts > max_ts:
            max_ts = ts
    return inserted, max_ts


def ingest_queries(conn: sqlite3.Connection, client: PiholeClient,
                  overlap_s: int = 120) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='last_ts'").fetchone()
    last_ts = float(row["value"]) if row else time.time() - 3600
    since = max(0.0, last_ts - overlap_s)
    ip_to_mac = {r["ip"]: r["mac"] for r in conn.execute("SELECT ip, mac FROM ip_map").fetchall()}
    cat_meta = _category_meta(conn)
    cache: dict[str, str | None] = {}
    inserted, max_ts = _ingest_window(conn, client, since, None, cat_meta, cache,
                                      ip_to_mac, make_alerts=True, alerts_notified=0)
    max_ts = max(max_ts, last_ts)
    conn.execute("INSERT INTO meta(key,value) VALUES('last_ts',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(max_ts),))
    conn.commit()
    return inserted


def backfill(conn: sqlite3.Connection, client: PiholeClient, days: int,
             progress=None) -> int:
    """Pull up to `days` of history, one day at a time. Alerts from the past are
    marked already-notified so they populate the UI without spamming webhooks."""
    refresh_devices(conn, client)
    ip_to_mac = {r["ip"]: r["mac"] for r in conn.execute("SELECT ip, mac FROM ip_map").fetchall()}
    cat_meta = _category_meta(conn)
    cache: dict[str, str | None] = {}
    now = time.time()
    total = 0
    for d in range(days):
        until = now - d * 86400
        since = until - 86400
        ins, _ = _ingest_window(conn, client, since, until, cat_meta, cache,
                                ip_to_mac, make_alerts=True, alerts_notified=1)
        total += ins
        conn.commit()
        if progress:
            progress(d + 1, days, ins)
    return total


def notify_alerts(conn: sqlite3.Connection, max_age_s: int = 3600) -> int:
    """POST recent, un-notified alerts for minors to the webhook, then mark done."""
    rows = conn.execute(
        "SELECT a.id, a.ts, a.domain, a.category_key, a.risk_level, a.mac, "
        "       COALESCE(d.label, a.mac) device, d.device_type, "
        "       COALESCE(p.name, 'non assegnato') person, "
        "       k.label cat_label, COALESCE(k.critical,0) critical "
        "FROM alert a "
        "LEFT JOIN device d ON d.mac = a.mac "
        "LEFT JOIN person p ON p.id = d.person_id "
        "LEFT JOIN category k ON k.key = a.category_key "
        "WHERE a.notified = 0 AND (p.kind = 'minor' OR k.critical = 1)").fetchall()
    now = time.time()
    sent = 0
    for r in rows:
        if settings.webhook_url and (now - r["ts"]) <= max_age_s:
            title = ("famboard - possibile disagio" if r["critical"]
                     else "famboard - avviso minore")
            dtype = f", {r['device_type']}" if r["device_type"] else ""
            msg = (f"{r['person']} — {r['device']} [{r['mac']}{dtype}]\n"
                   f"ha raggiunto '{r['cat_label']}': {r['domain']}")
            if r["critical"]:
                msg += "\nValuta un confronto sereno con la persona."
            prio = "high" if (r["critical"] or r["risk_level"] >= 2) else "default"
            if notify.push(msg, title, prio):
                sent += 1
        conn.execute("UPDATE alert SET notified = 1 WHERE id = ?", (r["id"],))
    conn.commit()
    return sent


# FTL numeric code maps, for importing straight from the FTL database.
_FTL_TYPE = {1: "A", 2: "AAAA", 3: "ANY", 4: "SRV", 5: "SOA", 6: "PTR", 7: "TXT",
             8: "NAPTR", 9: "MX", 10: "DS", 11: "RRSIG", 12: "DNSKEY", 13: "NS",
             14: "OTHER", 15: "SVCB", 16: "HTTPS"}
_FTL_STATUS = {0: "UNKNOWN", 1: "GRAVITY", 2: "FORWARD", 3: "CACHE", 4: "REGEX",
               5: "DENYLIST", 6: "EXTERNAL_BLOCKED_IP", 7: "EXTERNAL_BLOCKED_NULL",
               8: "EXTERNAL_BLOCKED_NXRA", 9: "GRAVITY_CNAME", 10: "REGEX_CNAME",
               11: "DENYLIST_CNAME", 12: "RETRIED", 13: "RETRIED_DNSSEC",
               14: "IN_PROGRESS", 15: "DBBUSY", 16: "SPECIAL_DOMAIN",
               17: "CACHE_STALE", 18: "EXTERNAL_BLOCKED_EDE15"}
_FTL_BLOCKED = {1, 4, 5, 6, 7, 8, 9, 10, 11, 16, 18}


def import_ftl_stream(conn: sqlite3.Connection, lines, batch_size: int = 5000,
                      progress=None) -> int:
    """Import rows piped from the FTL `queries` view.

    Each line is pipe-delimited: id|timestamp|type|status|domain|client.
    Dedupes against existing events by id; classifies as it goes.
    """
    ip_to_mac = {r["ip"]: r["mac"] for r in conn.execute("SELECT ip, mac FROM ip_map").fetchall()}
    cache: dict[str, str | None] = {}
    buf: list[tuple] = []
    seen = inserted = 0

    def flush():
        nonlocal inserted
        if buf:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO query_event"
                "(id, ts, client_ip, mac, domain, qtype, status, blocked, category_key) "
                "VALUES(?,?,?,?,?,?,?,?,?)", buf)
            inserted += cur.rowcount
            conn.commit()
            buf.clear()

    for raw in lines:
        parts = raw.rstrip("\n").split("|")
        if len(parts) < 6:
            continue
        try:
            qid = int(parts[0]); ts = float(parts[1])
            typ = int(parts[2]); status = int(parts[3])
        except ValueError:
            continue
        # FTL may store non-UTF8 / malformed domains; scrub lone surrogates
        # so SQLite (which requires valid UTF-8) doesn't choke.
        client_ip = parts[-1].encode("utf-8", "replace").decode("utf-8")
        domain = "|".join(parts[4:-1]).encode("utf-8", "replace").decode("utf-8")
        if not domain:
            continue
        category = classify_domain(conn, domain, cache)
        buf.append((qid, ts, client_ip, ip_to_mac.get(client_ip), domain,
                    _FTL_TYPE.get(typ, str(typ)), _FTL_STATUS.get(status, str(status)),
                    1 if status in _FTL_BLOCKED else 0, category))
        seen += 1
        if len(buf) >= batch_size:
            flush()
            if progress and seen % 200000 == 0:
                progress(seen, inserted)
    flush()
    return inserted


def run_cycle(conn: sqlite3.Connection, client: PiholeClient) -> dict:
    n_dev = refresh_devices(conn, client)
    n_q = ingest_queries(conn, client)
    n_notified = notify_alerts(conn)
    result = {"devices": n_dev, "new_queries": n_q, "notified": n_notified}
    # Event-driven attention report, re-evaluated at most every
    # ATTENTION_CHECK_INTERVAL seconds (the send itself is further gated).
    try:
        now = time.time()
        checked = float(db.get_meta(conn, "attention_checked_ts", "0") or 0)
        if now - checked >= settings.attention_check_interval:
            from . import report
            db.set_meta(conn, "attention_checked_ts", str(now))
            conn.commit()
            result["attention"] = report.dispatch_attention(conn, now=now)
    except Exception as exc:
        print(f"[attention] error: {exc}")
    # Access-control enforcement (opt-in: meta flag 'enforce' = '1').
    row = conn.execute("SELECT value FROM meta WHERE key='enforce'").fetchone()
    if row and row["value"] == "1":
        try:
            from . import enforce
            result["enforce"] = enforce.reconcile(conn, client)
        except Exception as exc:
            print(f"[enforce] error: {exc}")
    return result
