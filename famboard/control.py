"""Internet-access control: rule model, scheduling logic, desired block state.

Pure logic (no Pi-hole, no DB side effects beyond simple CRUD helpers) so the
scheduling can be unit-tested. The enforcer (enforce.py) turns the desired
state into Pi-hole group/client assignments.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime

WEEKDAYS_IT = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _in_window(now: datetime, win: dict) -> bool:
    days = win.get("days")
    start = _hm(win["start"])
    end = _hm(win["end"])
    cur = now.hour * 60 + now.minute
    wd = now.weekday()
    if start <= end:  # same-day window
        in_time = start <= cur < end
        day_ok = (days is None) or (wd in days)
        return in_time and day_ok
    # overnight window (e.g. 22:00-07:00): belongs to the day it STARTS on
    if cur >= start:
        return (days is None) or (wd in days)
    if cur < end:
        yesterday = (wd - 1) % 7
        return (days is None) or (yesterday in days)
    return False


def _windows(rule) -> list[dict]:
    raw = rule["windows"] if isinstance(rule, (dict, sqlite3.Row)) else None
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def _today_midnight(now_ts: float) -> float:
    dt = datetime.fromtimestamp(now_ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.timestamp()


def quota_consumed_minutes(conn, macs, target: str, now_ts: float) -> int:
    """Approximate today's usage as the number of distinct 1-minute buckets in
    which the device (any of its MACs) made a query — for the target category,
    or any domain for 'all'."""
    if isinstance(macs, str):
        macs = [macs]
    if not macs:
        return 0
    mid = _today_midnight(now_ts)
    ph = ",".join("?" * len(macs))
    if target == "all":
        row = conn.execute(
            f"SELECT COUNT(DISTINCT CAST(ts/60 AS INT)) n FROM query_event "
            f"WHERE mac IN ({ph}) AND ts >= ?", (*macs, mid)).fetchone()
    else:
        row = conn.execute(
            f"SELECT COUNT(DISTINCT CAST(ts/60 AS INT)) n FROM query_event "
            f"WHERE mac IN ({ph}) AND category_key = ? AND ts >= ?",
            (*macs, target, mid)).fetchone()
    return (row["n"] if row else 0) or 0


def rule_blocking_now(rule, now_ts: float | None = None, conn=None) -> bool:
    if not rule["enabled"]:
        return False
    now_ts = time.time() if now_ts is None else now_ts
    mode = rule["mode"]
    if mode == "block":
        return True
    if mode == "once":
        if rule["from_ts"] is not None and now_ts < rule["from_ts"]:
            return False
        if rule["until_ts"] is not None and now_ts >= rule["until_ts"]:
            return False
        return True
    if mode == "quota":
        quota = rule["quota_minutes"]
        if conn is None or not quota:
            return False
        from .devices import member_macs_for_mac
        macs = member_macs_for_mac(conn, rule["mac"])
        return quota_consumed_minutes(conn, macs, rule["target"], now_ts) >= quota
    now = datetime.fromtimestamp(now_ts)
    wins = _windows(rule)
    if mode == "schedule":
        return any(_in_window(now, w) for w in wins)
    if mode == "allow_window":
        return not any(_in_window(now, w) for w in wins)
    return False


def rule_expired(rule, now_ts: float | None = None) -> bool:
    """A one-off rule whose window has fully passed can be retired."""
    now_ts = time.time() if now_ts is None else now_ts
    return (rule["mode"] == "once" and rule["until_ts"] is not None
            and now_ts >= rule["until_ts"])


def desired_state(conn: sqlite3.Connection, now_ts: float | None = None) -> dict[str, dict]:
    """Return {mac: {'all': bool, 'cats': set[str]}} for rules blocking right now."""
    from .devices import member_macs_for_mac
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM control_rule WHERE enabled = 1").fetchall():
        if not rule_blocking_now(r, now_ts, conn=conn):
            continue
        # a rule on one MAC applies to every alias of the logical device
        for mac in member_macs_for_mac(conn, r["mac"]):
            slot = out.setdefault(mac, {"all": False, "cats": set()})
            if r["target"] == "all":
                slot["all"] = True
            else:
                slot["cats"].add(r["target"])
    return out


def retire_expired(conn: sqlite3.Connection, now_ts: float | None = None) -> int:
    rows = conn.execute(
        "SELECT * FROM control_rule WHERE enabled = 1 AND mode = 'once' "
        "AND until_ts IS NOT NULL").fetchall()
    n = 0
    for r in rows:
        if rule_expired(r, now_ts):
            conn.execute("UPDATE control_rule SET enabled = 0 WHERE id = ?", (r["id"],))
            n += 1
    if n:
        conn.commit()
    return n


# -- CRUD / quick actions --------------------------------------------------
def add_rule(conn: sqlite3.Connection, mac: str, target: str, mode: str,
             from_ts: float | None = None, until_ts: float | None = None,
             windows: list[dict] | None = None, quota_minutes: int | None = None,
             note: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO control_rule(mac, target, mode, from_ts, until_ts, windows, "
        "quota_minutes, note) VALUES(?,?,?,?,?,?,?,?)",
        (mac, target, mode, from_ts, until_ts,
         json.dumps(windows) if windows else None, quota_minutes, note))
    conn.commit()
    return cur.lastrowid


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    conn.execute("DELETE FROM control_rule WHERE id = ?", (rule_id,))
    conn.commit()


def rules_for(conn: sqlite3.Connection, mac: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM control_rule WHERE mac = ? ORDER BY created_at DESC",
        (mac,)).fetchall()]


def _target_label(target: str) -> str:
    if target == "all":
        return "Tutta la rete"
    from .taxonomy import CATEGORIES
    cat = CATEGORIES.get(target)
    return f"Categoria: {cat[0]}" if cat else f"Categoria: {target}"


def describe(rule, conn=None, now_ts: float | None = None) -> str:
    tgt = _target_label(rule["target"])
    mode = rule["mode"]
    if mode == "block":
        when = "bloccato sempre (finché non lo togli)"
    elif mode == "once":
        frm = ("subito" if not rule["from_ts"]
               else "dal " + datetime.fromtimestamp(rule["from_ts"]).strftime("%d/%m %H:%M"))
        to = ("a tempo indeterminato" if not rule["until_ts"]
              else "fino al " + datetime.fromtimestamp(rule["until_ts"]).strftime("%d/%m %H:%M"))
        when = f"bloccato {frm} {to}"
    elif mode == "quota":
        q = rule["quota_minutes"] or 0
        used = ""
        if conn is not None:
            from .devices import member_macs_for_mac
            macs = member_macs_for_mac(conn, rule["mac"])
            c = quota_consumed_minutes(conn, macs, rule["target"],
                                       now_ts or time.time())
            used = f" · usati oggi ~{c}/{q} min"
        when = f"max {q} min al giorno (stima da utilizzo){used}"
    elif mode in ("schedule", "allow_window"):
        parts = []
        for w in _windows(rule):
            days = ("ogni giorno" if not w.get("days")
                    else " ".join(WEEKDAYS_IT[d] for d in w["days"]))
            parts.append(f"{w['start']}–{w['end']} ({days})")
        wtxt = "; ".join(parts) or "—"
        when = (f"bloccato negli orari: {wtxt}" if mode == "schedule"
                else f"consentito SOLO negli orari: {wtxt} (bloccato fuori)")
    else:
        when = mode
    active = "🔴 attivo ora" if rule_blocking_now(rule, now_ts, conn=conn) else "⚪ non attivo ora"
    return f"{tgt} — {when} · {active}"
