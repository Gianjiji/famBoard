"""Functional + regression: ingestion, alerting, FTL import."""
import io
import time

from famboard import analytics, ingest


class QueryFake:
    def __init__(self, rows):
        self._rows = rows

    def queries(self, since_ts, until_ts=None):
        yield from self._rows


def _ingest(conn, rows):
    cat_meta = ingest._category_meta(conn)
    ipm = {r["ip"]: r["mac"] for r in conn.execute("SELECT ip,mac FROM ip_map")}
    return ingest._ingest_window(conn, QueryFake(rows), time.time() - 10,
                                 time.time() + 10, cat_meta, {}, ipm, True, 0)


def test_blocked_status_mapping(conn):
    conn.execute("INSERT INTO ip_map(ip,mac) VALUES('192.168.1.5','aa:bb:cc:dd:ee:05')")
    conn.commit()
    now = time.time()
    _ingest(conn, [
        {"id": 1, "time": now, "type": "A", "status": "GRAVITY", "domain": "ads.example",
         "client": {"ip": "192.168.1.5"}},
        {"id": 2, "time": now, "type": "A", "status": "FORWARD", "domain": "news.example",
         "client": {"ip": "192.168.1.5"}},
    ])
    rows = {r["id"]: r["blocked"] for r in conn.execute("SELECT id,blocked FROM query_event")}
    assert rows[1] == 1 and rows[2] == 0


def test_alert_only_for_reached_sensitive(conn):
    conn.execute("INSERT INTO ip_map(ip,mac) VALUES('192.168.1.6','aa:bb:cc:dd:ee:06')")
    conn.commit()
    now = time.time()
    _ingest(conn, [
        # same adult domain: one reached (FORWARD) -> alert, one blocked -> no alert
        {"id": 10, "time": now, "type": "A", "status": "FORWARD", "domain": "onlyfans.com",
         "client": {"ip": "192.168.1.6"}},
        {"id": 11, "time": now, "type": "A", "status": "GRAVITY", "domain": "onlyfans.com",
         "client": {"ip": "192.168.1.6"}},
    ])
    ids = [r["id"] for r in conn.execute("SELECT id FROM alert")]
    assert ids == [10]


def test_critical_alert_for_anyone(conn):
    # unassigned device reaching a distress domain -> alert regardless of age
    conn.execute("INSERT INTO device(mac) VALUES('aa:bb:cc:dd:ee:07')")
    conn.execute("INSERT INTO ip_map(ip,mac) VALUES('192.168.1.7','aa:bb:cc:dd:ee:07')")
    conn.commit()
    _ingest(conn, [{"id": 20, "time": time.time(), "type": "A", "status": "FORWARD",
                    "domain": "samaritans.org", "client": {"ip": "192.168.1.7"}}])
    crit = analytics.critical_alerts(conn, time.time() - 100)
    assert any(a["cat_key"] == "self_harm" for a in crit)
    assert analytics.open_alert_count(conn) >= 1


def test_ftl_import_sanitizes_and_dedupes(conn):
    # line with an invalid UTF-8 surrogate in the domain must not crash
    bad = "www.ex\udca2mple.com"
    lines = [
        f"500|{time.time()}|1|2|{bad}|192.168.1.8",
        f"501|{time.time()}|1|1|ads.example|192.168.1.8",   # status 1 = GRAVITY (blocked)
        f"500|{time.time()}|1|2|dup.example|192.168.1.8",   # duplicate id -> ignored
    ]
    n = ingest.import_ftl_stream(conn, io.StringIO("\n".join(lines)))
    assert n == 2  # 2 unique ids inserted, duplicate ignored
    blocked = conn.execute("SELECT blocked FROM query_event WHERE id=501").fetchone()["blocked"]
    assert blocked == 1  # FTL status code 1 -> blocked
