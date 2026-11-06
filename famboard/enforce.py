"""Turn the desired block state into Pi-hole group/client assignments.

Model:
- One group `famboard-blockall` carries a deny regex `.*` (everything). A device
  that must be fully offline is assigned to ONLY this group.
- One group per blocked category `famboard-cat-<key>` carries deny regexes for
  that category's well-known domains. A device blocked for a category is assigned
  to [Default(0), that group] so only the category is denied.

SAFETY: the `.*` regex is only ever attached to the blockall group, never to the
Default group, so the rest of the network is never affected.
"""
from __future__ import annotations

import re
import sqlite3

from . import control, kidsblock
from .taxonomy import CURATED

BLOCKALL_GROUP = "famboard-blockall"
CAT_PREFIX = "famboard-cat-"
COMMENT = "famboard"
BLOCKALL_REGEX = "(^|\\.).*$"  # matches every domain


def _cat_domains(cat: str) -> list[str]:
    return sorted({d for d, c in CURATED.items() if c == cat})


def _regex_for(domain: str) -> str:
    return "(^|\\.)" + re.escape(domain) + "$"


def desired_client_groups(conn: sqlite3.Connection, group_ids: dict,
                          kids: dict | None = None,
                          now_ts: float | None = None) -> dict[str, list[int]]:
    """{mac: [group ids]} for every MAC that has an enabled control rule or a
    kids blocklist. Kids groups are added on top of any category blocking."""
    desired = control.desired_state(conn, now_ts)
    managed = {r["mac"] for r in conn.execute(
        "SELECT DISTINCT mac FROM control_rule WHERE enabled = 1").fetchall()}
    out: dict[str, list[int]] = {}
    for mac in managed:
        slot = desired.get(mac)
        if slot and slot["all"]:
            out[mac] = [group_ids["blockall"]]
        elif slot and slot["cats"]:
            gids = {0}
            for c in slot["cats"]:
                if c in group_ids["cat"]:
                    gids.add(group_ids["cat"][c])
            out[mac] = sorted(gids)
        else:
            out[mac] = [0]  # has rules but none active now -> allowed

    # layer the per-minor kids group onto that minor's devices
    kids_gids = group_ids.get("kids", {})
    for pid, info in (kids or {}).items():
        gid = kids_gids.get(pid)
        if not gid:
            continue
        for mac in info["macs"]:
            if out.get(mac) == [group_ids["blockall"]]:
                continue  # already fully offline
            base = set(out.get(mac) or [0])
            base.update({0, gid})
            out[mac] = sorted(base)
    return out


def _ensure_group(client, groups: dict, name: str, comment: str) -> int:
    if name not in groups:
        client.create_group(name, comment)
        for g in client.list_groups():
            groups[g["name"]] = g["id"]
    return groups[name]


def _ensure_regex(client, existing: list[dict], pattern: str, gid: int) -> None:
    for d in existing:
        if d.get("domain") == pattern:
            if gid not in (d.get("groups") or []):
                # entry exists but not in our group; add our group to it
                client.add_regex_deny(pattern, sorted(set((d.get("groups") or []) + [gid])))
            return
    client.add_regex_deny(pattern, [gid])
    existing.append({"domain": pattern, "groups": [gid]})


def _ensure_kids(client, kids: dict, groups: dict, kids_ids: dict) -> bool:
    """Ensure a group per minor with its selected blocklists (adlists)
    subscribed. Returns True if adlist membership changed (gravity needed)."""
    existing = {a.get("address"): a for a in client.list_adlists()}
    want: dict[str, set[int]] = {}
    for pid, info in kids.items():
        gid = _ensure_group(client, groups, info["group"],
                            f"famboard: blocklist minore {pid}")
        kids_ids[pid] = gid
        for url in info["urls"]:
            want.setdefault(url, set()).add(gid)
    gravity = False
    for url, gids in want.items():
        cur = existing.get(url)
        if cur is None:
            client.add_adlist(url, sorted(gids)); gravity = True
        elif not gids <= set(cur.get("groups") or []):
            client.update_adlist(url, sorted(set(cur.get("groups") or []) | gids))
            gravity = True
    return gravity


def ensure_groups(client, needed_cats: set[str], kids: dict | None = None) -> dict:
    groups = {g["name"]: g["id"] for g in client.list_groups()}
    regex = client.list_regex_deny()
    blockall = _ensure_group(client, groups, BLOCKALL_GROUP, "famboard: blocca tutta la rete")
    _ensure_regex(client, regex, BLOCKALL_REGEX, blockall)
    cat_ids: dict[str, int] = {}
    for cat in needed_cats:
        gid = _ensure_group(client, groups, CAT_PREFIX + cat, f"famboard: blocca {cat}")
        for dom in _cat_domains(cat):
            _ensure_regex(client, regex, _regex_for(dom), gid)
        cat_ids[cat] = gid
    kids_ids: dict[int, int] = {}
    gravity = _ensure_kids(client, kids or {}, groups, kids_ids) if kids else False
    return {"blockall": blockall, "cat": cat_ids, "kids": kids_ids, "gravity": gravity}


def reconcile(conn: sqlite3.Connection, client, now_ts: float | None = None) -> dict:
    """Make the Pi-hole match the desired block state.

    Clients are managed by their CURRENT IP (from ip_map) because Pi-hole
    resolves a query's group by source IP; IPs are refreshed every cycle.
    Only devices that are blocking *right now* get a famboard client; the rest
    are cleaned up so they fall back to the Default group. A DNS restart is
    triggered on any change so it takes effect immediately (and flushes cache).
    """
    control.retire_expired(conn, now_ts)
    needed_cats = {r["target"] for r in conn.execute(
        "SELECT DISTINCT target FROM control_rule WHERE enabled = 1 AND target != 'all'").fetchall()}
    kids = kidsblock.desired(conn)
    group_ids = ensure_groups(client, needed_cats, kids)
    by_mac = desired_client_groups(conn, group_ids, kids, now_ts)

    mac_to_ips: dict[str, list[str]] = {}
    for r in conn.execute("SELECT ip, mac FROM ip_map").fetchall():
        mac_to_ips.setdefault(r["mac"], []).append(r["ip"])

    # Only currently-blocking devices get a client, keyed by their current IP(s).
    desired_ip: dict[str, list[int]] = {}
    for mac, gids in by_mac.items():
        if gids == [0]:
            continue
        for ip in mac_to_ips.get(mac, []):
            desired_ip[ip] = gids

    existing = {c.get("client"): c for c in client.list_clients()}
    changed = 0
    for ip, gids in desired_ip.items():
        cur = existing.get(ip)
        if cur is None:
            client.add_client(ip, gids)
            changed += 1
        elif (sorted(cur.get("groups") or []) != sorted(gids)
              or cur.get("comment") != COMMENT):
            # claim ownership (comment) so cleanup can release it later
            client.update_client(ip, gids)
            changed += 1

    # Remove famboard-managed clients that should no longer be blocked.
    for key, c in existing.items():
        if c.get("comment") == COMMENT and key not in desired_ip:
            client.delete_client(key)
            changed += 1

    if group_ids.get("gravity"):
        try:
            client.run_gravity()  # make newly-subscribed kids blocklists bite
        except Exception as exc:
            print(f"[enforce] gravity failed: {exc}")
    if changed:
        try:
            client.restart_dns()
        except Exception as exc:
            print(f"[enforce] restart_dns failed: {exc}")

    return {"blocking_ips": len(desired_ip), "changed": changed,
            "kids_groups": len(group_ids.get("kids", {}))}


def teardown(client) -> dict:
    """Remove every Pi-hole artifact famboard created (for a clean uninstall)."""
    removed = {"clients": 0, "regex": 0, "adlists": 0, "groups": 0}
    for c in client.list_clients():
        if c.get("comment") == COMMENT:
            client.delete_client(c["client"]); removed["clients"] += 1
    for d in client.list_regex_deny():
        if d.get("comment") == COMMENT or d.get("domain") == BLOCKALL_REGEX:
            client.delete_regex_deny(d["domain"]); removed["regex"] += 1
    for a in client.list_adlists():
        if a.get("comment") == COMMENT:
            client.delete_adlist(a["address"]); removed["adlists"] += 1
    for g in client.list_groups():
        if str(g.get("name", "")).startswith("famboard"):
            client.delete_group(g["name"]); removed["groups"] += 1
    if removed["adlists"]:
        try:
            client.run_gravity()
        except Exception:
            pass
    if any(removed.values()):
        try:
            client.restart_dns()
        except Exception:
            pass
    return removed
