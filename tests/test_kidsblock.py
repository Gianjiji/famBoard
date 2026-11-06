"""Kids blocklists: per-minor selection + enforcement into a dedicated group."""
from famboard import kidsblock, enforce


def _minor_with_device(conn, name, mac, ip=None):
    conn.execute("INSERT INTO person(name, kind) VALUES(?, 'minor')", (name,))
    pid = conn.execute("SELECT id FROM person WHERE name=?", (name,)).fetchone()["id"]
    conn.execute("INSERT INTO device(mac, person_id) VALUES(?,?)", (mac, pid))
    if ip:
        conn.execute("INSERT INTO ip_map(ip, mac) VALUES(?,?)", (ip, mac))
    conn.commit()
    return pid


def test_selection_roundtrip_ignores_unknown(conn):
    pid = _minor_with_device(conn, "Teo", "aa:bb:cc:00:00:01")
    kidsblock.set_selection(conn, pid, ["porn", "gambling", "bogus"])
    assert kidsblock.selected(conn, pid) == {"porn", "gambling"}
    kidsblock.set_selection(conn, pid, ["porn"])  # replaces, not appends
    assert kidsblock.selected(conn, pid) == {"porn"}


def test_desired_only_minors_with_lists_and_devices(conn):
    pid = _minor_with_device(conn, "Gaia", "aa:bb:cc:00:00:02")
    # adult with a list selected must NOT appear (only minors)
    conn.execute("INSERT INTO person(name, kind) VALUES('Papa','adult')")
    conn.commit()
    assert kidsblock.desired(conn) == {}            # nothing selected yet
    kidsblock.set_selection(conn, pid, ["porn"])
    d = kidsblock.desired(conn)
    assert set(d) == {pid}
    assert d[pid]["group"] == f"famboard-kids-{pid}"
    assert d[pid]["macs"] == ["aa:bb:cc:00:00:02"]
    assert kidsblock.CATALOG["porn"][1] in d[pid]["urls"]


def test_enforce_subscribes_adlist_to_minor_group_and_runs_gravity(conn, fake_pihole):
    pid = _minor_with_device(conn, "Lia", "aa:bb:cc:00:00:03", ip="192.168.1.80")
    kidsblock.set_selection(conn, pid, ["porn"])
    res = enforce.reconcile(conn, fake_pihole)
    # a per-minor group exists, the adlist is bound to it, gravity rebuilt
    gname = kidsblock.group_name(pid)
    gid = next(g["id"] for g in fake_pihole.groups if g["name"] == gname)
    assert res["kids_groups"] == 1
    assert fake_pihole.gravity_runs == 1
    assert fake_pihole.adlists and gid in fake_pihole.adlists[0]["groups"]
    # the minor's device IP is a client assigned to Default + the kids group
    client = next(c for c in fake_pihole.clients if c["client"] == "192.168.1.80")
    assert gid in client["groups"] and 0 in client["groups"]


def test_enforce_second_run_no_duplicate_gravity(conn, fake_pihole):
    pid = _minor_with_device(conn, "Mia", "aa:bb:cc:00:00:04", ip="192.168.1.81")
    kidsblock.set_selection(conn, pid, ["porn"])
    enforce.reconcile(conn, fake_pihole)
    enforce.reconcile(conn, fake_pihole)  # nothing changed
    assert fake_pihole.gravity_runs == 1  # not rebuilt again


def test_teardown_removes_adlists(conn, fake_pihole):
    pid = _minor_with_device(conn, "Nina", "aa:bb:cc:00:00:05", ip="192.168.1.82")
    kidsblock.set_selection(conn, pid, ["porn", "social"])
    enforce.reconcile(conn, fake_pihole)
    assert fake_pihole.adlists
    removed = enforce.teardown(fake_pihole)
    assert removed["adlists"] == 2 and fake_pihole.adlists == []
