"""Functional: analytics aggregates and weekly report."""
import time

from famboard import analytics, report


def _seed(conn):
    conn.execute("INSERT INTO person(name,kind) VALUES('Anna','minor')")
    pid = conn.execute("SELECT id FROM person").fetchone()["id"]
    conn.execute("INSERT INTO device(mac,person_id) VALUES('aa:bb:cc:dd:ee:30',?)", (pid,))
    now = time.time()
    data = [("youtube.com", "entertainment", 0), ("facebook.com", "social", 0),
            ("ads.example", None, 1), ("onlyfans.com", "adult", 0)]
    for i, (dom, cat, blk) in enumerate(data):
        conn.execute("INSERT INTO query_event(id,ts,mac,domain,category_key,blocked) "
                     "VALUES(?,?,?,?,?,?)", (40 + i, now - 60, "aa:bb:cc:dd:ee:30", dom, cat, blk))
    conn.commit()
    return pid, now


def test_overview_counts(conn):
    _seed(conn)
    ov = analytics.overview(conn, time.time() - 3600)
    assert ov["total"] == 4 and ov["blocked"] == 1 and ov["classified"] == 3


def test_category_breakdown(conn):
    _seed(conn)
    cats = {c["key"]: c["n"] for c in analytics.category_breakdown(conn, time.time() - 3600)}
    assert cats.get("entertainment") == 1 and cats.get("social") == 1


def test_minor_alert_for_reached_adult(conn):
    pid, now = _seed(conn)
    # adult reached by a minor should surface
    al = analytics.minor_alerts(conn, now - 3600)
    # minor_alerts reads query_event sensitive categories for minors
    assert any(a["cat_key"] == "adult" for a in al)


def test_report_build_and_render(conn):
    _seed(conn)
    rep = report.build(conn, days=7)
    assert rep["people"] and rep["people"][0]["name"] == "Anna"
    txt = report.render_text(rep)
    assert "Anna" in txt
    assert "<h" in report.render_html(rep)
