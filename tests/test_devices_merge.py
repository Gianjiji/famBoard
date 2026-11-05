"""Functional + regression: logical devices (merging randomized MACs)."""
from famboard import analytics, control, devices


def _two(conn):
    conn.execute("INSERT INTO person(name,kind) VALUES('Marco','minor')")
    pid = conn.execute("SELECT id FROM person").fetchone()["id"]
    conn.execute("INSERT INTO device(mac,vendor,person_id) VALUES('a0:11:22:33:44:55','Apple',?)", (pid,))
    conn.execute("INSERT INTO device(mac) VALUES('6a:bb:cc:dd:ee:ff')")
    conn.execute("INSERT INTO ip_map(ip,mac) VALUES('192.168.1.60','a0:11:22:33:44:55'),"
                 "('192.168.1.61','6a:bb:cc:dd:ee:ff')")
    conn.commit()
    a = conn.execute("SELECT id FROM device WHERE mac='a0:11:22:33:44:55'").fetchone()["id"]
    b = conn.execute("SELECT id FROM device WHERE mac='6a:bb:cc:dd:ee:ff'").fetchone()["id"]
    return pid, a, b


def test_merge_groups_macs(conn):
    pid, a, b = _two(conn)
    devices.merge(conn, b, a)
    assert set(devices.member_macs(conn, a)) == {"a0:11:22:33:44:55", "6a:bb:cc:dd:ee:ff"}
    assert devices.canonical_id(conn, b) == a


def test_person_macs_expand_across_aliases(conn):
    pid, a, b = _two(conn)
    devices.merge(conn, b, a)
    macs = set(analytics.device_macs_for_person(conn, pid))
    assert macs == {"a0:11:22:33:44:55", "6a:bb:cc:dd:ee:ff"}


def test_device_list_shows_only_canonical(conn):
    pid, a, b = _two(conn)
    devices.merge(conn, b, a)
    macs = [d["mac"] for d in analytics.devices(conn)]
    assert "a0:11:22:33:44:55" in macs
    assert "6a:bb:cc:dd:ee:ff" not in macs  # alias hidden


def test_rule_applies_to_all_aliases(conn):
    pid, a, b = _two(conn)
    devices.merge(conn, b, a)
    control.add_rule(conn, "a0:11:22:33:44:55", "all", "block")
    ds = control.desired_state(conn)
    assert ds.get("a0:11:22:33:44:55", {}).get("all") is True
    assert ds.get("6a:bb:cc:dd:ee:ff", {}).get("all") is True


def test_unmerge_restores(conn):
    pid, a, b = _two(conn)
    devices.merge(conn, b, a)
    devices.unmerge(conn, b)
    assert devices.canonical_id(conn, b) == b
    assert "6a:bb:cc:dd:ee:ff" in [d["mac"] for d in analytics.devices(conn)]
