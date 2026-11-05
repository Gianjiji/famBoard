"""Logical devices: group several MACs (e.g. a phone's randomized MACs) under
one canonical device that carries the label / person / type.

A device row with merged_into set is an alias of the canonical device it points
to. History, control rules and quotas apply across all member MACs.
"""
from __future__ import annotations

import sqlite3
import time


def canonical_id(conn: sqlite3.Connection, dev_id: int) -> int:
    row = conn.execute("SELECT id, merged_into FROM device WHERE id = ?", (dev_id,)).fetchone()
    if not row:
        return dev_id
    return row["merged_into"] or row["id"]


def member_devices(conn: sqlite3.Connection, canonical_dev_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM device WHERE id = ? OR merged_into = ? ORDER BY last_seen DESC",
        (canonical_dev_id, canonical_dev_id)).fetchall()]


def member_macs(conn: sqlite3.Connection, canonical_dev_id: int) -> list[str]:
    return [r["mac"] for r in conn.execute(
        "SELECT mac FROM device WHERE id = ? OR merged_into = ?",
        (canonical_dev_id, canonical_dev_id)).fetchall()]


def member_macs_for_mac(conn: sqlite3.Connection, mac: str) -> list[str]:
    row = conn.execute("SELECT id, merged_into FROM device WHERE mac = ?", (mac,)).fetchone()
    if not row:
        return [mac]
    return member_macs(conn, row["merged_into"] or row["id"])


def merge(conn: sqlite3.Connection, child_id: int, parent_id: int) -> None:
    """Merge device child_id into parent_id (parent resolved to its canonical)."""
    if child_id == parent_id:
        return
    prow = conn.execute("SELECT id, merged_into FROM device WHERE id = ?", (parent_id,)).fetchone()
    if not prow:
        return
    parent_canon = prow["merged_into"] or prow["id"]
    if parent_canon == child_id:  # avoid a cycle
        return
    # the child loses its own owner/alias role; its children re-home to the parent
    conn.execute(
        "UPDATE device SET merged_into = ?, person_id = NULL, is_shared = 0 WHERE id = ?",
        (parent_canon, child_id))
    conn.execute("UPDATE device SET merged_into = ? WHERE merged_into = ?",
                 (parent_canon, child_id))
    conn.commit()


def unmerge(conn: sqlite3.Connection, child_id: int) -> None:
    conn.execute("UPDATE device SET merged_into = NULL WHERE id = ?", (child_id,))
    conn.commit()


def _is_random(mac: str) -> bool:
    return len(mac) > 1 and mac[1].lower() in ("2", "6", "a", "e")


def suggestions(conn: sqlite3.Connection, limit: int = 8) -> list[dict]:
    """Heuristic 'maybe same device' pairs among randomized, canonical devices,
    ranked by overlap of their most-queried domains. Suggestive only."""
    cans = [dict(r) for r in conn.execute(
        "SELECT id, mac, label, last_seen FROM device WHERE merged_into IS NULL").fetchall()
        if _is_random(r["mac"])]
    prints: dict[int, set] = {}
    for d in cans:
        rows = conn.execute(
            "SELECT domain FROM query_event WHERE mac = ? GROUP BY domain "
            "ORDER BY COUNT(*) DESC LIMIT 40", (d["mac"],)).fetchall()
        prints[d["id"]] = {r["domain"] for r in rows}
    out = []
    for i in range(len(cans)):
        for j in range(i + 1, len(cans)):
            a, b = cans[i], cans[j]
            fa, fb = prints[a["id"]], prints[b["id"]]
            if not fa or not fb:
                continue
            inter = len(fa & fb)
            union = len(fa | fb)
            sim = inter / union if union else 0
            if sim >= 0.4:
                out.append({"a": a, "b": b, "similarity": round(sim, 2), "shared": inter})
    out.sort(key=lambda x: -x["similarity"])
    return out[:limit]


def prune_stale(conn: sqlite3.Connection, client=None, days: int = 30,
                now_ts: float | None = None, dry_run: bool = False) -> dict:
    """Remove devices not seen on the network for `days`, from the famboard DB
    AND from the Pi-hole network table (the poller re-imports anything the
    Pi-hole still lists). A merge group is kept if ANY member is recent, and a
    device the Pi-hole saw recently is never touched. Query history and alerts
    are kept; stale ip_map entries and control rules are dropped.
    """
    now = now_ts if now_ts is not None else time.time()
    cutoff = now - days * 86400

    groups: dict[int, dict] = {}
    for d in conn.execute(
            "SELECT id, mac, merged_into, last_seen FROM device").fetchall():
        canon = d["merged_into"] or d["id"]
        g = groups.setdefault(canon, {"ids": [], "macs": [], "eff": 0})
        g["ids"].append(d["id"])
        g["macs"].append(d["mac"].lower())
        g["eff"] = max(g["eff"], d["last_seen"] or 0)

    # never delete a group the Pi-hole itself saw recently
    remote: dict[str, dict] = {}
    if client is not None:
        for d in client.network_devices():
            mac = (d.get("hwaddr") or "").lower()
            if mac:
                remote[mac] = d
    for g in groups.values():
        g["eff"] = max([g["eff"]] + [(remote.get(m, {}).get("lastQuery") or 0)
                                     for m in g["macs"]])

    stale_ids: list[int] = []
    stale_macs: list[str] = []
    for g in groups.values():
        if g["eff"] < cutoff:
            stale_ids += g["ids"]
            stale_macs += g["macs"]

    if dry_run:
        return {"removed": len(stale_ids), "removed_remote": 0, "macs": stale_macs}

    removed_remote = 0
    if client is not None:
        for mac in stale_macs:
            d = remote.get(mac)
            if d is not None and "id" in d:
                client.delete_network_device(d["id"])
                removed_remote += 1

    if stale_ids:
        qi = ",".join("?" * len(stale_ids))
        qm = ",".join("?" * len(stale_macs))
        conn.execute(f"DELETE FROM control_rule WHERE lower(mac) IN ({qm})", stale_macs)
        conn.execute(f"DELETE FROM ip_map WHERE lower(mac) IN ({qm})", stale_macs)
        conn.execute(f"DELETE FROM device WHERE id IN ({qi})", stale_ids)
    conn.commit()
    return {"removed": len(stale_ids), "removed_remote": removed_remote,
            "macs": stale_macs}
