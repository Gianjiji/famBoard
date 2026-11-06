"""Attention scoring + event-driven report dispatch."""
import time

from famboard import attention, report, db

NOW = time.time()


def _mk_person(conn, name, kind, mac):
    conn.execute("INSERT INTO person(name, kind) VALUES(?,?)", (name, kind))
    pid = conn.execute("SELECT id FROM person WHERE name=?", (name,)).fetchone()["id"]
    conn.execute("INSERT INTO device(mac, person_id) VALUES(?,?)", (mac, pid))
    conn.commit()
    return pid


def _hits(conn, mac, cat, n, domains=1, spread_days=1, blocked=0, recent=True):
    base = NOW - (3600 if recent else 10 * 86400)
    for i in range(n):
        dom = f"d{i % domains}.{cat}.example"
        ts = base - (i % spread_days) * 86400
        conn.execute("INSERT INTO query_event(ts, mac, domain, category_key, blocked) "
                     "VALUES(?,?,?,?,?)", (ts, mac, dom, cat, blocked))
    conn.commit()


def test_one_off_sensitive_is_low(conn):
    _mk_person(conn, "Teo", "minor", "aa:bb:cc:00:00:01")
    _hits(conn, "aa:bb:cc:00:00:01", "adult", n=1)
    subs = attention.score_subjects(conn, now=NOW)
    teo = [s for s in subs if s["name"] == "Teo"]
    # a single hit either stays below threshold or at most 'watch', never auto
    assert not teo or teo[0]["level"] not in attention.AUTO_LEVELS


def test_recurring_varied_sensitive_escalates(conn):
    _mk_person(conn, "Gaia", "minor", "aa:bb:cc:00:00:02")
    _hits(conn, "aa:bb:cc:00:00:02", "adult", n=60, domains=15, spread_days=7)
    subs = attention.score_subjects(conn, now=NOW)
    gaia = next(s for s in subs if s["name"] == "Gaia")
    assert gaia["level"] in attention.AUTO_LEVELS
    assert any("Varietà di domini" in r for r in gaia["reasons"])


def test_critical_flags_anyone_with_reason(conn):
    _mk_person(conn, "Papa", "adult", "aa:bb:cc:00:00:03")
    _hits(conn, "aa:bb:cc:00:00:03", "self_harm", n=4, domains=2, spread_days=3)
    s = next(x for x in attention.score_subjects(conn, now=NOW) if x["name"] == "Papa")
    assert s["has_critical"]
    assert any("critica" in r.lower() for r in s["reasons"])


def test_unassigned_device_is_scored(conn):
    conn.execute("INSERT INTO device(mac, label) VALUES('aa:bb:cc:00:00:04','TabletX')")
    conn.commit()
    _hits(conn, "aa:bb:cc:00:00:04", "adult", n=40, domains=10, spread_days=6)
    subs = attention.score_subjects(conn, now=NOW)
    dev = next(s for s in subs if s["name"] == "TabletX")
    assert dev["kind"] == "device" and dev["level"] in attention.AUTO_LEVELS


def test_dispatch_sends_once_then_dedupes(conn):
    _mk_person(conn, "Lia", "minor", "aa:bb:cc:00:00:05")
    _hits(conn, "aa:bb:cc:00:00:05", "adult", n=50, domains=12, spread_days=7)
    first = report.dispatch_attention(conn, now=NOW)
    assert first["sent"] is True and first["flagged"] >= 1
    # identical situation -> no update -> not sent again
    again = report.dispatch_attention(conn, now=NOW + 60)
    assert again["sent"] is False and again["reason"] == "no change"


def test_dispatch_resends_when_situation_changes(conn):
    _mk_person(conn, "Mia", "minor", "aa:bb:cc:00:00:06")
    _hits(conn, "aa:bb:cc:00:00:06", "adult", n=50, domains=12, spread_days=7)
    report.dispatch_attention(conn, now=NOW)
    # a new critical category appears -> signature changes -> sends again
    _hits(conn, "aa:bb:cc:00:00:06", "self_harm", n=6, domains=3, spread_days=3)
    later = report.dispatch_attention(conn, now=NOW + report.settings.attention_min_interval + 1)
    assert later["sent"] is True


def test_force_bypasses_dedup(conn):
    _mk_person(conn, "Nina", "minor", "aa:bb:cc:00:00:07")
    _hits(conn, "aa:bb:cc:00:00:07", "adult", n=50, domains=12, spread_days=7)
    report.dispatch_attention(conn, now=NOW)
    forced = report.dispatch_attention(conn, force=True, now=NOW + 60)
    assert forced["sent"] is True and forced["forced"] is True


def test_dispatch_nothing_when_quiet(conn):
    _mk_person(conn, "Quiet", "minor", "aa:bb:cc:00:00:08")
    _hits(conn, "aa:bb:cc:00:00:08", "social", n=5)  # not sensitive
    res = report.dispatch_attention(conn, now=NOW)
    assert res["sent"] is False and res["flagged"] == 0
