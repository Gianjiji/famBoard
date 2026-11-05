"""Functional + regression: access-control scheduling and quota logic."""
import time
from datetime import datetime

from famboard import control


def _ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi).timestamp()


def _rule(conn, mac="aa:bb:cc:dd:ee:01"):
    return conn.execute("SELECT * FROM control_rule WHERE mac=? ORDER BY id DESC",
                        (mac,)).fetchone()


def test_block_always(conn):
    control.add_rule(conn, "aa:bb:cc:dd:ee:01", "all", "block")
    assert control.rule_blocking_now(_rule(conn)) is True


def test_disabled_rule_never_blocks(conn):
    rid = control.add_rule(conn, "aa:bb:cc:dd:ee:01", "all", "block")
    conn.execute("UPDATE control_rule SET enabled=0 WHERE id=?", (rid,))
    conn.commit()
    assert control.rule_blocking_now(_rule(conn)) is False


def test_once_delayed_then_open_ended(conn):
    now = _ts(2026, 11, 4, 18, 0)
    control.add_rule(conn, "aa:bb:cc:dd:ee:01", "all", "once",
                     from_ts=now + 3600, until_ts=None)
    r = _rule(conn)
    assert control.rule_blocking_now(r, now + 1800) is False   # not yet
    assert control.rule_blocking_now(r, now + 5400) is True    # after start


def test_allow_window_netflix(conn):
    # allowed ONLY 16-17 -> blocked outside
    control.add_rule(conn, "aa:bb:cc:dd:ee:01", "entertainment", "allow_window",
                     windows=[{"start": "16:00", "end": "17:00"}])
    r = _rule(conn)
    assert control.rule_blocking_now(r, _ts(2026, 11, 4, 16, 30)) is False
    assert control.rule_blocking_now(r, _ts(2026, 11, 4, 12, 0)) is True


def test_schedule_overnight(conn):
    control.add_rule(conn, "aa:bb:cc:dd:ee:01", "all", "schedule",
                     windows=[{"start": "22:00", "end": "07:00"}])
    r = _rule(conn)
    assert control.rule_blocking_now(r, _ts(2026, 11, 4, 23, 0)) is True
    assert control.rule_blocking_now(r, _ts(2026, 11, 4, 3, 0)) is True
    assert control.rule_blocking_now(r, _ts(2026, 11, 4, 14, 0)) is False


def test_quota_daily(conn):
    mac = "aa:bb:cc:dd:ee:02"
    conn.execute("INSERT INTO device(mac) VALUES(?)", (mac,))
    mid = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    for i in range(65):  # 65 distinct active minutes today
        conn.execute("INSERT INTO query_event(id,ts,mac,domain,category_key,blocked) "
                     "VALUES(?,?,?,?,?,0)", (3000 + i, mid + 8 * 3600 + i * 60, mac,
                                             f"m{i}.facebook.com", "social"))
    conn.commit()
    control.add_rule(conn, mac, "social", "quota", quota_minutes=60)
    r = conn.execute("SELECT * FROM control_rule WHERE mac=?", (mac,)).fetchone()
    assert control.rule_blocking_now(r, conn=conn) is True   # 65 >= 60
    conn.execute("UPDATE control_rule SET quota_minutes=200 WHERE mac=?", (mac,))
    conn.commit()
    r = conn.execute("SELECT * FROM control_rule WHERE mac=?", (mac,)).fetchone()
    assert control.rule_blocking_now(r, conn=conn) is False  # 65 < 200


def test_desired_state_aggregates(conn):
    conn.execute("INSERT INTO device(mac) VALUES('aa:bb:cc:dd:ee:03')")
    conn.commit()
    control.add_rule(conn, "aa:bb:cc:dd:ee:03", "all", "block")
    ds = control.desired_state(conn)
    assert ds["aa:bb:cc:dd:ee:03"]["all"] is True


def test_retire_expired(conn):
    now = time.time()
    control.add_rule(conn, "aa:bb:cc:dd:ee:04", "all", "once",
                     until_ts=now - 10)  # already past
    assert control.retire_expired(conn, now) == 1
    row = conn.execute("SELECT enabled FROM control_rule WHERE mac='aa:bb:cc:dd:ee:04'").fetchone()
    assert row["enabled"] == 0
