"""Kids blocklists: per-minor subscriptions to curated family blocklists that
apply ONLY to that minor's devices.

A minor profile can opt into one or more catalog lists. Enforcement subscribes
the chosen lists as Pi-hole adlists bound to a per-minor group
`famboard-kids-<pid>`, and assigns that minor's device IPs to the group — so
the blocklist bites only on their devices, nothing else on the network.

The catalog points at well-known, maintained hosts lists. Applying them needs
the enforcement switch ON (see enforce.py) and rebuilds Pi-hole gravity.
"""
from __future__ import annotations

import sqlite3

GROUP_PREFIX = "famboard-kids-"

# key -> (label, adlist URL, short description)
CATALOG: dict[str, tuple[str, str, str]] = {
    "porn": (
        "Contenuti per adulti",
        "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/porn/hosts",
        "Blocca i principali siti pornografici (lista StevenBlack).",
    ),
    "gambling": (
        "Gioco d'azzardo",
        "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/gambling/hosts",
        "Blocca scommesse e casinò online (lista StevenBlack).",
    ),
    "fakenews": (
        "Disinformazione",
        "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/fakenews/hosts",
        "Blocca fonti note di disinformazione (lista StevenBlack).",
    ),
    "social": (
        "Social network",
        "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/social/hosts",
        "Blocca i principali social network (lista StevenBlack).",
    ),
}


def catalog() -> list[dict]:
    return [{"key": k, "label": v[0], "url": v[1], "desc": v[2]}
            for k, v in CATALOG.items()]


def selected(conn: sqlite3.Connection, person_id: int) -> set[str]:
    return {r["list_key"] for r in conn.execute(
        "SELECT list_key FROM kid_list WHERE person_id = ?", (person_id,)).fetchall()}


def set_selection(conn: sqlite3.Connection, person_id: int, keys: list[str]) -> None:
    """Replace a minor's chosen lists with `keys` (unknown keys ignored)."""
    valid = [k for k in keys if k in CATALOG]
    conn.execute("DELETE FROM kid_list WHERE person_id = ?", (person_id,))
    conn.executemany("INSERT OR IGNORE INTO kid_list(person_id, list_key) VALUES(?,?)",
                     [(person_id, k) for k in valid])
    conn.commit()


def group_name(person_id: int) -> str:
    return f"{GROUP_PREFIX}{person_id}"


def desired(conn: sqlite3.Connection) -> dict[int, dict]:
    """{person_id: {"group": name, "urls": [adlist urls], "macs": [device macs]}}
    for every minor that has at least one list enabled and at least one device."""
    from . import analytics
    out: dict[int, dict] = {}
    rows = conn.execute(
        "SELECT p.id, p.name FROM person p WHERE p.kind = 'minor' "
        "AND EXISTS (SELECT 1 FROM kid_list k WHERE k.person_id = p.id)").fetchall()
    for p in rows:
        keys = selected(conn, p["id"])
        macs = analytics.device_macs_for_person(conn, p["id"])
        if not keys or not macs:
            continue
        out[p["id"]] = {"group": group_name(p["id"]),
                        "urls": [CATALOG[k][1] for k in sorted(keys)],
                        "macs": macs}
    return out
