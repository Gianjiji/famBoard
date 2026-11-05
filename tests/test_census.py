"""Census CSV sync and stale-device pruning."""
import csv
import time

from famboard import census, devices

NOW = time.time()
DAY = 86400


def _dev(conn, mac, label=None, dtype=None, last_seen=None, merged_into=None):
    conn.execute(
        "INSERT INTO device(mac, label, device_type, last_seen, merged_into) "
        "VALUES(?,?,?,?,?)", (mac, label, dtype, last_seen, merged_into))
    conn.commit()
    return conn.execute("SELECT id FROM device WHERE mac=?", (mac,)).fetchone()["id"]


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM like exports
        w = csv.DictWriter(f, fieldnames=census.FIELDS)
        w.writeheader()
        w.writerows(rows)


def test_import_fills_only_missing_labels(conn, tmp_path):
    _dev(conn, "aa:aa:aa:aa:aa:01")                       # no label -> filled
    _dev(conn, "aa:aa:aa:aa:aa:02", label="MioNome")      # labelled -> kept
    p = tmp_path / "census.csv"
    _write_csv(p, [
        {"name": "Lampada", "type": "IoT", "mac_address": "AA:AA:AA:AA:AA:01",
         "ip_address": "192.168.1.5"},
        {"name": "AltroNome", "type": "IoT", "mac_address": "aa:aa:aa:aa:aa:02",
         "ip_address": ""},
        {"name": "MaiVisto", "type": "IoT", "mac_address": "aa:aa:aa:aa:aa:03",
         "ip_address": ""},
        {"name": "Unknown Device", "type": "Unknown",
         "mac_address": "aa:aa:aa:aa:aa:04", "ip_address": ""},
    ])
    res = census.import_csv(conn, p)
    assert res["updated"] == 1 and res["added"] == 1
    assert res["conflicts"] == [("aa:aa:aa:aa:aa:02", "MioNome", "AltroNome")]
    row = conn.execute("SELECT label, device_type FROM device "
                       "WHERE mac='aa:aa:aa:aa:aa:01'").fetchone()
    assert (row["label"], row["device_type"]) == ("Lampada", "IoT")
    assert conn.execute("SELECT label FROM device WHERE mac='aa:aa:aa:aa:aa:02'"
                        ).fetchone()["label"] == "MioNome"
    assert conn.execute("SELECT label FROM device WHERE mac='aa:aa:aa:aa:aa:03'"
                        ).fetchone()["label"] == "MaiVisto"
    assert conn.execute("SELECT COUNT(*) c FROM device "
                        "WHERE mac='aa:aa:aa:aa:aa:04'").fetchone()["c"] == 0


def test_export_mirrors_labelled_non_alias_devices(conn, tmp_path):
    a = _dev(conn, "bb:bb:bb:bb:bb:01", label="Principale", dtype="IoT")
    _dev(conn, "bb:bb:bb:bb:bb:02", label="Alias", merged_into=a)
    _dev(conn, "bb:bb:bb:bb:bb:03")  # unlabelled -> excluded
    conn.execute("INSERT INTO ip_map(ip, mac) VALUES('192.168.1.9','bb:bb:bb:bb:bb:01')")
    conn.commit()
    p = tmp_path / "census.csv"
    assert census.export_csv(conn, p) == 1
    rows = list(csv.DictReader(open(p, newline="")))
    assert rows == [{"name": "Principale", "type": "IoT",
                     "mac_address": "bb:bb:bb:bb:bb:01", "ip_address": "192.168.1.9"}]


def test_sync_roundtrip_rewrites_csv_from_db(conn, tmp_path):
    _dev(conn, "cc:cc:cc:cc:cc:01", label="NelDB", dtype="IoT")
    p = tmp_path / "census.csv"
    _write_csv(p, [{"name": "SoloCSV", "type": "IoT",
                    "mac_address": "cc:cc:cc:cc:cc:02", "ip_address": ""}])
    res = census.sync(conn, p)
    assert res["added"] == 1 and res["exported"] == 2
    names = {r["name"] for r in csv.DictReader(open(p, newline=""))}
    assert names == {"NelDB", "SoloCSV"}


def test_prune_removes_stale_keeps_recent_groups(conn, fake_pihole):
    old = NOW - 40 * DAY
    _dev(conn, "dd:dd:dd:dd:dd:01", label="Vecchio", last_seen=old)
    _dev(conn, "dd:dd:dd:dd:dd:02", label="Recente", last_seen=NOW - DAY)
    canon = _dev(conn, "dd:dd:dd:dd:dd:03", label="CanonVecchio", last_seen=old)
    _dev(conn, "dd:dd:dd:dd:dd:04", merged_into=canon, last_seen=NOW - DAY)
    conn.execute("INSERT INTO ip_map(ip, mac) VALUES('10.0.0.1','dd:dd:dd:dd:dd:01')")
    conn.execute("INSERT INTO control_rule(mac, target, mode) "
                 "VALUES('dd:dd:dd:dd:dd:01','all','block')")
    conn.commit()
    fake_pihole.net_devices = [
        {"id": 7, "hwaddr": "dd:dd:dd:dd:dd:01", "lastQuery": old}]

    res = devices.prune_stale(conn, fake_pihole, days=30, now_ts=NOW)
    assert res["removed"] == 1 and res["removed_remote"] == 1
    assert res["macs"] == ["dd:dd:dd:dd:dd:01"]
    assert fake_pihole.net_devices == []
    left = {r["mac"] for r in conn.execute("SELECT mac FROM device")}
    # the alias group survives because one member is recent
    assert left == {"dd:dd:dd:dd:dd:02", "dd:dd:dd:dd:dd:03", "dd:dd:dd:dd:dd:04"}
    assert conn.execute("SELECT COUNT(*) c FROM control_rule").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM ip_map").fetchone()["c"] == 0


def test_prune_spares_device_recent_on_pihole_side(conn, fake_pihole):
    _dev(conn, "ee:ee:ee:ee:ee:01", label="StaleNelDB", last_seen=NOW - 40 * DAY)
    fake_pihole.net_devices = [
        {"id": 9, "hwaddr": "ee:ee:ee:ee:ee:01", "lastQuery": NOW - DAY}]
    res = devices.prune_stale(conn, fake_pihole, days=30, now_ts=NOW)
    assert res["removed"] == 0
    assert fake_pihole.net_devices  # untouched


def test_prune_dry_run_changes_nothing(conn):
    _dev(conn, "ff:ff:ff:ff:ff:01", label="Vecchio", last_seen=NOW - 40 * DAY)
    res = devices.prune_stale(conn, None, days=30, now_ts=NOW, dry_run=True)
    assert res["removed"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM device").fetchone()["c"] == 1
