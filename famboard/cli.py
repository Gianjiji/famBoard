"""Command line entry point: init the DB, run one ingest cycle, or poll."""
from __future__ import annotations

import argparse
import time

from pathlib import Path

from . import categories, census, db, devices, ingest, report, taxonomy
from .config import settings
from .pihole import PiholeClient


def cmd_init() -> None:
    settings.ensure_dirs()
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        taxonomy.seed_categories(conn)
    finally:
        conn.close()
    print(f"DB initialised at {settings.db_path}")
    print(f"Seeded {len(taxonomy.CATEGORIES)} categories")


def _client() -> PiholeClient:
    return PiholeClient(settings.pihole_url, settings.pihole_password,
                        verify_tls=settings.pihole_verify_tls)


def cmd_poll(once: bool) -> None:
    settings.ensure_dirs()
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    taxonomy.seed_categories(conn)
    try:
        with _client() as client:
            while True:
                t0 = time.time()
                stats = ingest.run_cycle(conn, client)
                print(f"[{time.strftime('%H:%M:%S')}] cycle: "
                      f"devices={stats['devices']} new_queries={stats['new_queries']} "
                      f"({time.time()-t0:.2f}s)")
                if once:
                    break
                time.sleep(settings.poll_interval)
    finally:
        conn.close()


def cmd_backfill(days: int) -> None:
    settings.ensure_dirs()
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    taxonomy.seed_categories(conn)

    def progress(done: int, total: int, ins: int) -> None:
        print(f"  giorno {done}/{total}: +{ins} eventi")

    try:
        with _client() as client:
            n = ingest.backfill(conn, client, days, progress=progress)
        print(f"backfill completato: {n:,} eventi storici importati")
    finally:
        conn.close()


def cmd_import_ftl() -> None:
    import sys
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    taxonomy.seed_categories(conn)

    def progress(seen: int, inserted: int) -> None:
        print(f"  lette {seen:,} · inserite {inserted:,}", flush=True)

    try:
        n = ingest.import_ftl_stream(conn, sys.stdin, progress=progress)
        print(f"import FTL completato: {n:,} eventi nuovi")
    finally:
        conn.close()


def cmd_report(days: int, send: bool) -> None:
    conn = db.connect(settings.db_path)
    try:
        rep = report.build(conn, days=days)
        print(report.render_text(rep))
        if send:
            res = report.deliver(rep)
            print(f"\nconsegna: webhook={res['webhook']} email={res['email']}")
    finally:
        conn.close()


def cmd_build_categories(tarball: str) -> None:
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    taxonomy.seed_categories(conn)
    try:
        counts = categories.build_index(conn, Path(tarball))
        total = sum(counts.values())
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {v:>9,}  {k}")
        print(f"domain->category index built: {total:,} entries")
        print("reclassifying stored events...")
        n = categories.reclassify(conn)
        print(f"updated {n:,} events")
    finally:
        conn.close()


def cmd_census_sync(csv_path: str | None) -> None:
    path = Path(csv_path) if csv_path else settings.census_csv
    conn = db.connect(settings.db_path)
    try:
        res = census.sync(conn, path)
        print(f"censimento: +{res['added']} nuovi, {res['updated']} etichettati, "
              f"{res['exported']} esportati in {path}")
        for mac, db_label, csv_name in res["conflicts"]:
            print(f"  conflitto (vince il DB): {mac} DB='{db_label}' CSV='{csv_name}'")
    finally:
        conn.close()


def cmd_prune(days: int, yes: bool) -> None:
    conn = db.connect(settings.db_path)
    try:
        if not yes:
            preview = devices.prune_stale(conn, None, days, dry_run=True)
            print(f"{preview['removed']} dispositivi non visti da {days} giorni "
                  f"verrebbero rimossi: {', '.join(preview['macs']) or 'nessuno'}")
            print("(nessuna modifica fatta: ripeti con --yes per procedere)")
            return
        with _client() as client:
            res = devices.prune_stale(conn, client, days)
        print(f"rimossi {res['removed']} dispositivi "
              f"({res['removed_remote']} anche dalla network table del Pi-hole)")
        n = census.export_csv(conn, settings.census_csv)
        print(f"CSV riallineato: {n} dispositivi in {settings.census_csv}")
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="famboard")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create DB and seed categories")
    sub.add_parser("poll-once", help="run a single ingest cycle")
    sub.add_parser("poll", help="run the ingest loop forever")
    bc = sub.add_parser("build-categories", help="load UT1 tarball into domain index")
    bc.add_argument("tarball", help="path to blacklists.tar.gz")
    sub.add_parser("reclassify", help="recompute categories for stored events")
    bf = sub.add_parser("backfill", help="import past history from the Pi-hole")
    bf.add_argument("--days", type=int, default=7)
    rp = sub.add_parser("report", help="per-person activity report")
    rp.add_argument("--days", type=int, default=7)
    rp.add_argument("--send", action="store_true", help="deliver via webhook/email")
    sub.add_parser("import-ftl", help="import FTL query rows piped on stdin")
    cs = sub.add_parser("census-sync", help="sync census CSV <-> DB (DB wins)")
    cs.add_argument("--csv", default=None, help="CSV path (default: CENSUS_CSV)")
    pr = sub.add_parser("prune", help="drop devices unseen for N days (DB + Pi-hole)")
    pr.add_argument("--days", type=int, default=30)
    pr.add_argument("--yes", action="store_true", help="apply (default: dry-run)")
    args = p.parse_args()
    if args.cmd == "init":
        cmd_init()
    elif args.cmd == "poll-once":
        cmd_poll(once=True)
    elif args.cmd == "poll":
        cmd_poll(once=False)
    elif args.cmd == "build-categories":
        cmd_build_categories(args.tarball)
    elif args.cmd == "backfill":
        cmd_backfill(args.days)
    elif args.cmd == "report":
        cmd_report(args.days, args.send)
    elif args.cmd == "import-ftl":
        cmd_import_ftl()
    elif args.cmd == "census-sync":
        cmd_census_sync(args.csv)
    elif args.cmd == "prune":
        cmd_prune(args.days, args.yes)
    elif args.cmd == "reclassify":
        conn = db.connect(settings.db_path)
        try:
            print(f"updated {categories.reclassify(conn):,} events")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
