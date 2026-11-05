"""Functional + SECURITY: Pi-hole enforcement reconciliation."""
from famboard import control, enforce


def _setup(conn, mac="aa:bb:cc:dd:ee:10", ip="192.168.1.90"):
    conn.execute("INSERT INTO device(mac) VALUES(?)", (mac,))
    conn.execute("INSERT INTO ip_map(ip,mac) VALUES(?,?)", (ip, mac))
    conn.commit()
    return mac, ip


def test_block_all_assigns_ip_to_blockall_group(conn, fake_pihole):
    mac, ip = _setup(conn)
    control.add_rule(conn, mac, "all", "block")
    res = enforce.reconcile(conn, fake_pihole)
    gid = {g["name"]: g["id"] for g in fake_pihole.groups}["famboard-blockall"]
    clients = {c["client"]: c["groups"] for c in fake_pihole.clients}
    assert clients[ip] == [gid]
    assert res["changed"] >= 1
    assert fake_pihole.restarts == 1  # restart triggered on change


def test_SECURITY_blockall_regex_never_in_default_group(conn, fake_pihole):
    mac, ip = _setup(conn)
    control.add_rule(conn, mac, "all", "block")
    enforce.reconcile(conn, fake_pihole)
    gid = {g["name"]: g["id"] for g in fake_pihole.groups}["famboard-blockall"]
    all_regex = [r for r in fake_pihole.regex if r["domain"] == enforce.BLOCKALL_REGEX]
    assert all_regex, "block-all regex must exist"
    for r in all_regex:
        assert set(r["groups"]) == {gid}   # ONLY blockall group
        assert 0 not in r["groups"]        # NEVER the Default group


def test_category_block_keeps_default_plus_category(conn, fake_pihole):
    mac, ip = _setup(conn)
    control.add_rule(conn, mac, "social", "block")
    enforce.reconcile(conn, fake_pihole)
    clients = {c["client"]: c["groups"] for c in fake_pihole.clients}
    assert 0 in clients[ip]                # keeps Default (rest of web allowed)
    assert len(clients[ip]) >= 2           # plus the category group


def test_unblock_removes_client_and_restarts(conn, fake_pihole):
    mac, ip = _setup(conn)
    control.add_rule(conn, mac, "all", "block")
    enforce.reconcile(conn, fake_pihole)
    conn.execute("DELETE FROM control_rule")
    conn.commit()
    enforce.reconcile(conn, fake_pihole)
    assert all(c["client"] != ip for c in fake_pihole.clients)
    assert fake_pihole.restarts == 2


def test_idempotent_no_restart_without_change(conn, fake_pihole):
    mac, ip = _setup(conn)
    control.add_rule(conn, mac, "all", "block")
    enforce.reconcile(conn, fake_pihole)
    before = fake_pihole.restarts
    enforce.reconcile(conn, fake_pihole)   # nothing changed
    assert fake_pihole.restarts == before
