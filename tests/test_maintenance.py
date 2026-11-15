"""Housekeeping: retention, compact domain index, planner hints.

Performance claims are checked the only way that stays true on any machine:
by asking SQLite which index it will use, not by timing it.
"""
import sqlite3
import time

import pytest

from famboard import analytics, cli, db, maintenance
from famboard.config import settings

DAY = 86400.0
NOW = 1_800_000_000.0
MAC_A, MAC_B = "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"


def fill_events(conn, days=120, per_day=40):
    """`per_day` events a day for `days` days, ending at NOW; ids are unique."""
    rows = []
    for d in range(days):
        for i in range(per_day):
            ts = NOW - d * DAY - i * 60
            rows.append((ts, f"site{(d * 7 + i) % 97}.example", MAC_A if i % 3 else MAC_B,
                         "games" if i % 5 == 0 else None, 1 if i % 11 == 0 else 0))
    conn.executemany(
        "INSERT INTO query_event(ts, domain, mac, category_key, blocked) VALUES(?,?,?,?,?)", rows)
    conn.commit()
    return rows


def count(conn, table="query_event"):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- retention --------------------------------------------------------------
def test_prune_removes_only_events_older_than_the_limit(conn):
    rows = fill_events(conn, days=120)
    removed = maintenance.prune_events(conn, 90, now=NOW)
    expected_kept = [r for r in rows if r[0] >= NOW - 90 * DAY]
    assert removed == len(rows) - len(expected_kept)
    assert count(conn) == len(expected_kept)
    assert conn.execute("SELECT MIN(ts) FROM query_event").fetchone()[0] >= NOW - 90 * DAY


def test_prune_boundary_is_exclusive(conn):
    conn.executemany("INSERT INTO query_event(ts, domain) VALUES(?, 'x.example')",
                     [(NOW - 30 * DAY - 1,), (NOW - 30 * DAY,), (NOW - 30 * DAY + 1,)])
    conn.commit()
    assert maintenance.prune_events(conn, 30, now=NOW) == 1
    assert count(conn) == 2


def test_prune_works_across_batches(conn):
    rows = fill_events(conn, days=60, per_day=20)
    removed = maintenance.prune_events(conn, 10, now=NOW, batch=37)
    assert removed == sum(1 for r in rows if r[0] < NOW - 10 * DAY)
    assert maintenance.prune_events(conn, 10, now=NOW, batch=37) == 0     # idempotent


@pytest.mark.parametrize("days", [0, -1, -365])
def test_retention_off_deletes_nothing(conn, days):
    fill_events(conn, days=30)
    before = count(conn)
    assert maintenance.prune_events(conn, days, now=NOW) == 0
    assert maintenance.run_retention(conn, days, now=NOW) is None
    assert count(conn) == before


def test_alerts_survive_retention(conn):
    fill_events(conn, days=120)
    conn.execute("INSERT INTO alert(id, ts, mac, domain, category_key) VALUES(1, ?, ?, 'x', 'adult')",
                 (NOW - 110 * DAY, MAC_A))
    conn.commit()
    maintenance.prune_events(conn, 30, now=NOW)
    assert count(conn, "alert") == 1


def test_events_older_than_matches_what_prune_removes(conn):
    fill_events(conn, days=100)
    preview = maintenance.events_older_than(conn, 45, now=NOW)
    assert preview > 0
    assert maintenance.prune_events(conn, 45, now=NOW) == preview


def test_run_retention_is_at_most_daily(conn):
    fill_events(conn, days=100)
    first = maintenance.run_retention(conn, 60, now=NOW)
    assert first and first > 0
    assert maintenance.run_retention(conn, 60, now=NOW + 3600) is None           # same day
    conn.execute("INSERT INTO query_event(ts, domain) VALUES(?, 'old.example')", (NOW - 80 * DAY,))
    conn.commit()
    removed = maintenance.run_retention(conn, 60, now=NOW + DAY + 1)            # next day
    assert removed is not None and removed >= 1
    assert conn.execute(
        "SELECT COUNT(*) FROM query_event WHERE domain='old.example'").fetchone()[0] == 0
    assert conn.execute("SELECT MIN(ts) FROM query_event").fetchone()[0] >= NOW + DAY + 1 - 60 * DAY


def test_retention_is_off_by_default():
    """Deploying this version must not start deleting anyone's history."""
    assert settings.retention_days == 0


def test_poll_cycle_applies_retention_when_configured(conn, fake_pihole, monkeypatch):
    from famboard import ingest
    old, recent = time.time() - 100 * DAY, time.time() - DAY
    conn.executemany("INSERT INTO query_event(ts, domain) VALUES(?, 'x.example')",
                     [(old,), (recent,)])
    conn.commit()
    monkeypatch.setattr(ingest, "refresh_devices", lambda c, cl: 0)
    monkeypatch.setattr(ingest, "ingest_queries", lambda c, cl: 0)
    monkeypatch.setattr(ingest, "notify_alerts", lambda c: 0)

    assert "pruned" not in ingest.run_cycle(conn, fake_pihole)                  # default: off
    assert count(conn) == 2

    object.__setattr__(settings, "retention_days", 30)
    try:
        assert ingest.run_cycle(conn, fake_pihole)["pruned"] == 1
    finally:
        object.__setattr__(settings, "retention_days", 0)
    assert [r[0] for r in conn.execute("SELECT ts FROM query_event")] == [recent]


# --- compact domain index ---------------------------------------------------
OLD_LAYOUT = """
CREATE TABLE domain_category (
    domain       TEXT PRIMARY KEY,
    category_key TEXT NOT NULL REFERENCES category(key),
    source       TEXT
)
"""


def old_layout_conn(conn):
    """Give `conn` the table as databases created before this change have it."""
    rows = conn.execute("SELECT domain, category_key, source FROM domain_category").fetchall()
    conn.execute("DROP TABLE domain_category")
    conn.execute(OLD_LAYOUT)
    conn.executemany("INSERT INTO domain_category VALUES(?,?,?)", [tuple(r) for r in rows])
    conn.executemany("INSERT INTO domain_category VALUES(?,?,?)",
                     [(f"d{i}.example", "games", "test") for i in range(3000)])
    conn.commit()
    return conn


def test_new_database_has_the_compact_layout(conn):
    assert maintenance.domain_category_is_compact(conn)


def test_old_layout_is_recognised_and_rebuilt(conn):
    old_layout_conn(conn)
    assert not maintenance.domain_category_is_compact(conn)
    before = conn.execute(
        "SELECT domain, category_key, source FROM domain_category ORDER BY domain").fetchall()

    assert maintenance.compact_domain_category(conn) is True
    assert maintenance.domain_category_is_compact(conn)
    after = conn.execute(
        "SELECT domain, category_key, source FROM domain_category ORDER BY domain").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert maintenance.compact_domain_category(conn) is False            # nothing left to do
    leftovers = conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'domain_category_%'").fetchall()
    assert leftovers == []


def test_rebuilt_table_keeps_its_constraints(conn):
    old_layout_conn(conn)
    maintenance.compact_domain_category(conn)
    with pytest.raises(sqlite3.IntegrityError):          # primary key
        conn.execute("INSERT INTO domain_category VALUES('d1.example', 'games', 'dup')")
    with pytest.raises(sqlite3.IntegrityError):          # foreign key to category
        conn.execute("INSERT INTO domain_category VALUES('new.example', 'no-such-category', 't')")
    conn.rollback()
    conn.execute("INSERT OR IGNORE INTO domain_category VALUES('d1.example', 'adult', 'dup')")
    assert conn.execute("SELECT category_key FROM domain_category WHERE domain='d1.example'"
                        ).fetchone()[0] == "games"


def test_classification_is_the_same_on_both_layouts(conn):
    from famboard.ingest import classify_domain
    old_layout_conn(conn)
    domains = ["d7.example", "sub.d7.example", "nowhere.invalid", "D7.EXAMPLE"]
    before = [classify_domain(conn, d, {}) for d in domains]
    maintenance.compact_domain_category(conn)
    assert [classify_domain(conn, d, {}) for d in domains] == before
    assert before[0] == "games"


def test_failed_rebuild_leaves_the_old_table_untouched(conn):
    old_layout_conn(conn)
    before = count(conn, "domain_category")
    conn.execute("CREATE TRIGGER boom BEFORE DELETE ON category BEGIN SELECT 1; END")
    conn.commit()

    class Boom(Exception):
        pass

    def deny(action, arg1, arg2, dbname, source):
        # refuse the final rename, the last step of the rebuild
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK

    conn.set_authorizer(deny)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            maintenance.compact_domain_category(conn)
    finally:
        conn.set_authorizer(None)
    assert not maintenance.domain_category_is_compact(conn)
    assert count(conn, "domain_category") == before
    assert maintenance.compact_domain_category(conn) is True             # and it can be retried


def test_init_db_leaves_an_old_layout_working(tmp_path):
    """Starting the new version on an old file must neither fail nor rebuild it."""
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(db.SCHEMA.replace(") WITHOUT ROWID;", ");"))
    raw.execute("INSERT INTO category(key, label) VALUES('games', 'Giochi')")
    raw.execute("INSERT INTO domain_category VALUES('d1.example', 'games', 't')")
    raw.commit()
    raw.close()
    db.init_db(path)
    conn = db.connect(path)
    try:
        assert not maintenance.domain_category_is_compact(conn)
        assert count(conn, "domain_category") == 1
    finally:
        conn.close()


def test_optimize_reports_and_shrinks(tmp_path):
    path = tmp_path / "big.db"
    db.init_db(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO category(key, label) VALUES('games', 'Giochi')")
    conn.commit()
    old_layout_conn(conn)
    conn.executemany("INSERT INTO domain_category VALUES(?,?,?)",
                     [(f"a-rather-long-domain-name-{i}.example.org", "games", "ut1:games")
                      for i in range(40000)])
    conn.commit()
    res = maintenance.optimize(conn, vacuum=True)
    assert res["compacted"] is True and res["vacuumed"] is True
    assert res["bytes_after"] < res["bytes_before"] * 0.75, res
    assert count(conn, "domain_category") == 43000
    assert conn.execute("SELECT COUNT(*) FROM sqlite_stat1").fetchone()[0] > 0
    again = maintenance.optimize(conn, vacuum=False)
    assert again["compacted"] is False and again["vacuumed"] is False
    conn.close()


# --- command line -----------------------------------------------------------
def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["famboard", *argv])
    cli.main()
    return capsys.readouterr().out


@pytest.fixture
def settings_db():
    for suffix in ("", "-wal", "-shm"):
        try:
            (settings.db_path.parent / (settings.db_path.name + suffix)).unlink()
        except OSError:
            pass
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    yield conn
    conn.close()


def test_cli_prune_events_is_a_dry_run_by_default(settings_db, monkeypatch, capsys):
    old, recent = time.time() - 100 * DAY, time.time() - DAY
    settings_db.executemany("INSERT INTO query_event(ts, domain) VALUES(?, 'x.example')",
                            [(old,), (old - 5,), (recent,)])
    settings_db.commit()
    out = run_cli(monkeypatch, capsys, "prune-events", "--days", "30")
    assert "2 eventi" in out and "nessuna modifica" in out
    assert count(settings_db) == 3

    out = run_cli(monkeypatch, capsys, "prune-events", "--days", "30", "--yes")
    assert "eliminati 2 eventi" in out
    assert count(settings_db) == 1


def test_cli_prune_events_refuses_zero_days(settings_db, monkeypatch, capsys):
    settings_db.execute("INSERT INTO query_event(ts, domain) VALUES(1.0, 'x.example')")
    settings_db.commit()
    run_cli(monkeypatch, capsys, "prune-events", "--days", "0", "--yes")
    assert count(settings_db) == 1


def test_cli_optimize_db(settings_db, monkeypatch, capsys):
    out = run_cli(monkeypatch, capsys, "optimize-db")
    assert "già in forma compatta" in out and "MB" in out
    out = run_cli(monkeypatch, capsys, "optimize-db", "--no-vacuum")
    assert "VACUUM saltato" in out


# --- planner: time-window reads go through the ts index ---------------------
UNFILTERED = {
    "overview": lambda c, s: analytics.overview(c, s),
    "category_breakdown": lambda c, s: analytics.category_breakdown(c, s),
    "critical_alerts": lambda c, s: analytics.critical_alerts(c, s),
    "unassigned_sensitive": lambda c, s: analytics.unassigned_sensitive(c, s),
    "activity_timeline": lambda c, s: analytics.activity_timeline(c, s, 3600),
    "hourly_distribution": lambda c, s: analytics.hourly_distribution(c, s),
    "top_domains": lambda c, s: analytics.top_domains(c, s),
    "recent_events": lambda c, s: analytics.recent_events(c, s),
}


def plans_of(conn, call):
    """EXPLAIN QUERY PLAN of every statement `call` runs, as one string each."""
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        call()
    finally:
        conn.set_trace_callback(None)
    return [" | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall())
            for sql in statements if sql.lstrip().upper().startswith("SELECT")]


@pytest.mark.parametrize("with_statistics", [False, True])
@pytest.mark.parametrize("name", sorted(UNFILTERED))
def test_window_queries_use_the_time_index(conn, name, with_statistics):
    """Without the hint the planner walks every event ever stored, in the order
    of the domain (or mac) index, to save itself a sort; and ANALYZE does not
    change its mind in a reliable direction. Checked in both states."""
    fill_events(conn, days=120)
    if with_statistics:
        conn.execute("ANALYZE")
        conn.commit()
    plans = plans_of(conn, lambda: UNFILTERED[name](conn, NOW - DAY))
    assert plans, "no SELECT captured"
    for plan in plans:
        assert "idx_event_ts (ts>?)" in plan, plan
        assert "SCAN e" not in plan and "SCAN query_event" not in plan, plan


def test_the_planner_really_needs_the_hint(conn):
    """Control: the same top-domains query without the hint scans the table.
    If SQLite ever stops doing that, the hint can be dropped."""
    fill_events(conn, days=120)
    sql = ("EXPLAIN QUERY PLAN SELECT e.domain, COUNT(*) n FROM query_event e "
           "WHERE e.ts >= ? GROUP BY e.domain ORDER BY n DESC LIMIT 15")
    plan = " | ".join(r[3] for r in conn.execute(sql, (NOW - DAY,)).fetchall())
    assert "idx_event_domain" in plan and "idx_event_ts" not in plan, plan


def test_hinted_queries_return_the_right_numbers(conn):
    rows = fill_events(conn, days=120)
    since = NOW - 3 * DAY
    window = [r for r in rows if r[0] >= since]

    assert analytics.overview(conn, since) == {
        "total": len(window), "blocked": sum(r[4] for r in window),
        "classified": sum(1 for r in window if r[3])}

    expected = {}
    for r in window:
        expected[r[1]] = expected.get(r[1], 0) + 1
    top = analytics.top_domains(conn, since, limit=500)
    assert {t["domain"]: t["n"] for t in top} == expected
    assert [t["n"] for t in top] == sorted(expected.values(), reverse=True)

    only_b = analytics.top_domains(conn, since, macs=[MAC_B], limit=500)
    assert sum(t["n"] for t in only_b) == sum(1 for r in window if r[2] == MAC_B)
    games = analytics.top_domains(conn, since, cat_key="games", limit=500)
    assert sum(t["n"] for t in games) == sum(1 for r in window if r[3] == "games")

    timeline = analytics.activity_timeline(conn, since, 3600)
    assert sum(b["total"] for b in timeline) == len(window)
    assert sum(analytics.hourly_distribution(conn, since)) == len(window)
    assert analytics.top_domains(conn, since, macs=[]) == []


def test_filtered_queries_are_left_to_the_planner(conn):
    fill_events(conn, days=120)
    for call in (lambda: analytics.top_domains(conn, NOW - DAY, macs=[MAC_A]),
                 lambda: analytics.top_domains(conn, NOW - DAY, cat_key="games"),
                 lambda: analytics.recent_events(conn, NOW - DAY, macs=[MAC_A])):
        for plan in plans_of(conn, call):
            assert "SCAN e" not in plan, plan
