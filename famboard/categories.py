"""Build the domain->category index from the UT1 (Toulouse) blacklist tarball.

Reads category 'domains' files straight out of the tarball (no extraction to
disk, and only the members we mapped, validated by name). Sensitive categories
are loaded first and win on conflict, so a domain that appears in both e.g.
'search_engines' and 'adult' is flagged as adult.
"""
from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path
from typing import Iterator

from .taxonomy import CATEGORIES, UT1_TO_CATEGORY, seed_curated

BATCH = 20000


def _priority(ut1_name: str) -> tuple[int, int]:
    cat = CATEGORIES[UT1_TO_CATEGORY[ut1_name]]
    return (cat[2], cat[1])  # (minor_sensitive, risk_level): higher = loaded first


def _iter_domains(tar: tarfile.TarFile, ut1_name: str) -> Iterator[str]:
    member_name = f"blacklists/{ut1_name}/domains"
    try:
        member = tar.getmember(member_name)
    except KeyError:
        return
    if not member.isfile():
        return
    f = tar.extractfile(member)
    if f is None:
        return
    for raw in f:
        line = raw.decode("utf-8", "ignore").strip().lower().rstrip(".")
        if line and not line.startswith("#"):
            yield line


def build_index(conn: sqlite3.Connection, tarball_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    order = sorted(UT1_TO_CATEGORY, key=_priority, reverse=True)
    with tarfile.open(tarball_path, "r:gz") as tar:
        for ut1_name in order:
            cat = UT1_TO_CATEGORY[ut1_name]
            batch: list[tuple[str, str, str]] = []
            n = 0
            for domain in _iter_domains(tar, ut1_name):
                batch.append((domain, cat, f"ut1:{ut1_name}"))
                if len(batch) >= BATCH:
                    conn.executemany(
                        "INSERT OR IGNORE INTO domain_category(domain, category_key, source) "
                        "VALUES(?,?,?)", batch)
                    n += len(batch)
                    batch.clear()
            if batch:
                conn.executemany(
                    "INSERT OR IGNORE INTO domain_category(domain, category_key, source) "
                    "VALUES(?,?,?)", batch)
                n += len(batch)
            if n:
                counts[f"{ut1_name} -> {cat}"] = n
            conn.commit()
    counts["curated overlay"] = seed_curated(conn)
    return counts


def reclassify(conn: sqlite3.Connection) -> int:
    """Recompute category_key for all stored events (after rebuilding the index)."""
    from .ingest import classify_domain
    cache: dict[str, str | None] = {}
    domains = [r["domain"] for r in
               conn.execute("SELECT DISTINCT domain FROM query_event").fetchall()]
    updated = 0
    for domain in domains:
        cat = classify_domain(conn, domain, cache)
        if cat is None:
            continue
        cur = conn.execute(
            "UPDATE query_event SET category_key=? WHERE domain=?", (cat, domain))
        updated += cur.rowcount
    conn.commit()
    return updated
