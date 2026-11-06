"""Attention scoring: rank people and devices by how much their recent
sensitive/critical DNS activity warrants a closer look.

Pure analysis over data the Pi-hole already logs — the *domains* contacted,
never their content (HTTPS hides that, and packet capture would not recover
it). Per subject we combine signals that separate a one-off from a pattern:
  - volume & breadth of sensitive hits (log-scaled: diminishing returns)
  - how many distinct days (recurring vs a single burst)
  - night concentration (22:00-07:00)
  - trend (front-loaded in the recent half = growing)
Critical "distress" categories (self-harm, eating disorders, extremism) weigh
far more than merely age-sensitive ones, and are flagged for ANYONE, not only
minors. Every flagged subject carries human-readable reasons — the score is
explainable, never a black box.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import time

from . import analytics, devices as devmod

WINDOW_DAYS = 14
# score -> level thresholds (tunable; calibrated so a single accidental hit to
# a sensitive site stays 'watch', while recurring/varied/nightly or critical
# activity climbs to 'concern'/'high').
WATCH, CONCERN, HIGH = 6.0, 15.0, 30.0
AUTO_LEVELS = ("concern", "high")  # levels that trigger an automatic report

_PER_CAT_SQL = (
    "SELECT k.key, k.label, k.color, k.risk_level, COALESCE(k.critical,0) critical, "
    "       COUNT(*) n, COUNT(DISTINCT e.domain) domains, "
    "       COUNT(DISTINCT date(e.ts,'unixepoch','localtime')) days, "
    "       SUM(CASE WHEN CAST(strftime('%H', e.ts,'unixepoch','localtime') AS INT) >= 22 "
    "                 OR CAST(strftime('%H', e.ts,'unixepoch','localtime') AS INT) < 7 "
    "               THEN 1 ELSE 0 END) night, "
    "       SUM(CASE WHEN e.ts >= ? THEN 1 ELSE 0 END) recent, "
    "       MAX(e.ts) last_ts "
    "FROM query_event e JOIN category k ON k.key = e.category_key "
    "WHERE e.ts >= ? AND e.blocked = 0 AND (k.minor_sensitive = 1 OR k.critical = 1)"
)


def _severity(risk_level: int, critical: int) -> float:
    return 3.0 if critical else 1.0 + 0.5 * risk_level


def _level(score: float) -> str:
    if score >= HIGH:
        return "high"
    if score >= CONCERN:
        return "concern"
    if score >= WATCH:
        return "watch"
    return "none"


def _score_rows(rows: list[dict]) -> dict:
    """Combine per-category rows (one subject) into a score + breakdown."""
    total = sum(r["n"] for r in rows)
    recent = sum(r["recent"] for r in rows)
    night = sum(r["night"] for r in rows)
    domains = sum(r["domains"] for r in rows)
    days_active = max((r["days"] for r in rows), default=0)
    base = 0.0
    cats = []
    for r in rows:
        persist = min(r["days"], 7) / 7.0
        nightfrac = r["night"] / r["n"] if r["n"] else 0.0
        intensity = (math.log2(1 + r["n"]) + math.log2(1 + r["domains"])
                     + 2.0 * persist + 1.5 * nightfrac)
        cat_score = _severity(r["risk_level"], r["critical"]) * intensity
        base += cat_score
        cats.append({**r, "score": round(cat_score, 1)})
    older = total - recent
    trend = (recent - older) / total if total else 0.0  # -1..1, >0 = growing
    score = base * (1.0 + 0.5 * max(0.0, trend))
    cats.sort(key=lambda c: -c["score"])
    return {"score": round(score, 1), "level": _level(score), "categories": cats,
            "total": total, "night": night, "domains": domains,
            "days_active": days_active, "trend": round(trend, 2),
            "has_critical": any(r["critical"] for r in rows)}


def _reasons(s: dict) -> list[str]:
    out: list[str] = []
    crit = [c for c in s["categories"] if c["critical"]]
    if crit:
        out.append("Categoria critica raggiunta: "
                   + ", ".join(f"{c['label']} ({c['n']})" for c in crit))
    sens = [c for c in s["categories"] if not c["critical"]]
    if sens:
        out.append("Categorie sensibili: "
                   + ", ".join(f"{c['label']} ({c['n']})" for c in sens[:4]))
    if s["days_active"] >= 4:
        out.append(f"Attività ricorrente su {s['days_active']} giorni")
    if s["night"] >= 3:
        out.append(f"Concentrazione notturna: {s['night']} richieste (22–07)")
    if s["domains"] >= 5:
        out.append(f"Varietà di domini: {s['domains']} distinti")
    if s["trend"] > 0.3 and s["total"] >= 6:
        out.append("In aumento nell'ultimo periodo")
    return out


def _sig_one(s: dict) -> str:
    """Quantized per-subject signature: changes only on a meaningful shift
    (level, score bucket of 5, domain bucket of 5, or the set of categories),
    not on every extra request — so persistent-but-stable situations don't
    re-alert, while escalation does."""
    cats = ",".join(sorted(c["key"] for c in s["categories"]))
    raw = f"{s['kind']}:{s['id']}:{s['level']}:{int(s['score']//5)}:{int(s['domains']//5)}:{cats}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def situation_signature(subjects: list[dict]) -> str:
    joined = "|".join(sorted(x["signature"] for x in subjects))
    return hashlib.sha1(joined.encode()).hexdigest()[:16]


def _query_subject(conn: sqlite3.Connection, macs: list[str],
                   since: float, mid: float) -> list[dict]:
    if not macs:
        return []
    ph = ",".join("?" * len(macs))
    sql = _PER_CAT_SQL + f" AND e.mac IN ({ph}) GROUP BY k.key"
    return [dict(r) for r in conn.execute(sql, [mid, since, *macs]).fetchall()]


def score_subjects(conn: sqlite3.Connection, days: int = WINDOW_DAYS,
                   now: float | None = None) -> list[dict]:
    """Every person (with devices) and every unassigned device, scored and
    sorted by descending attention. Subjects below 'watch' are dropped."""
    now = now if now is not None else time.time()
    since = now - days * 86400
    mid = now - (days * 86400) / 2
    out: list[dict] = []

    for p in analytics.people(conn):
        if not p["n_devices"]:
            continue
        rows = _query_subject(conn, analytics.device_macs_for_person(conn, p["id"]), since, mid)
        s = _score_rows(rows) if rows else None
        if not s or s["level"] == "none":
            continue
        s.update(kind="person", id=p["id"], name=p["name"],
                 person_kind=p["kind"], link=f"/person/{p['id']}")
        s["reasons"] = _reasons(s)
        s["signature"] = _sig_one(s)
        out.append(s)

    for d in conn.execute("SELECT id, COALESCE(label, mac) name FROM device "
                          "WHERE merged_into IS NULL AND person_id IS NULL").fetchall():
        rows = _query_subject(conn, devmod.member_macs(conn, d["id"]), since, mid)
        s = _score_rows(rows) if rows else None
        if not s or s["level"] == "none":
            continue
        s.update(kind="device", id=d["id"], name=d["name"],
                 person_kind=None, link=f"/device/{d['id']}")
        s["reasons"] = _reasons(s)
        s["signature"] = _sig_one(s)
        out.append(s)

    out.sort(key=lambda x: -x["score"])
    return out
