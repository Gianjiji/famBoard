"""Census CSV <-> DB sync.

The CSV (default: censimentoMAC/) is a human-editable backup of the device
census. Import fills in labels the DB is missing and registers MACs the
Pi-hole has never seen; it never overwrites a label already set in the DB.
Export rewrites the CSV from the DB: the DB is the source of truth.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

FIELDS = ["name", "type", "mac_address", "ip_address"]
_UNKNOWN = "unknown device"


def import_csv(conn: sqlite3.Connection, path: Path | str) -> dict:
    """Fill missing labels from the CSV. Returns counts and kept conflicts."""
    added = updated = 0
    conflicts: list[tuple[str, str, str]] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            ctype = (row.get("type") or "").strip() or None
            mac = (row.get("mac_address") or "").strip().lower()
            if not mac or ":" not in mac or not name or name.lower() == _UNKNOWN:
                continue
            dev = conn.execute(
                "SELECT id, label FROM device WHERE mac = ?", (mac,)).fetchone()
            if dev is None:
                conn.execute(
                    "INSERT INTO device(mac, label, device_type) VALUES(?,?,?)",
                    (mac, name, ctype))
                added += 1
            elif dev["label"] is None:
                conn.execute(
                    "UPDATE device SET label = ?, device_type = ? WHERE id = ?",
                    (name, ctype, dev["id"]))
                updated += 1
            elif dev["label"] != name:
                conflicts.append((mac, dev["label"], name))
    conn.commit()
    return {"added": added, "updated": updated, "conflicts": conflicts}


def export_csv(conn: sqlite3.Connection, path: Path | str) -> int:
    """Rewrite the CSV with every labelled, non-alias device (DB wins)."""
    ipmap: dict[str, str] = {}
    for r in conn.execute("SELECT ip, mac FROM ip_map ORDER BY updated_at"):
        ipmap[r["mac"].lower()] = r["ip"]  # most recent assignment wins

    rows = []
    for d in conn.execute(
            "SELECT mac, label, device_type FROM device "
            "WHERE label IS NOT NULL AND merged_into IS NULL"):
        mac = d["mac"].lower()
        rows.append({"name": d["label"], "type": d["device_type"] or "Unknown",
                     "mac_address": mac, "ip_address": ipmap.get(mac, "")})

    def key(r: dict) -> tuple:
        try:
            last = int(r["ip_address"].rsplit(".", 1)[1])
        except (IndexError, ValueError):
            last = 999
        return (last, r["name"].lower())

    rows.sort(key=key)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def sync(conn: sqlite3.Connection, path: Path | str) -> dict:
    """Import what the CSV knows and the DB doesn't, then export DB -> CSV."""
    if Path(path).exists():
        res = import_csv(conn, path)
    else:
        res = {"added": 0, "updated": 0, "conflicts": []}
    res["exported"] = export_csv(conn, path)
    return res
