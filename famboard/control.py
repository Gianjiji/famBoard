"""Internet-access control: rule model, scheduling logic, desired block state.

Pure logic (no Pi-hole, no DB side effects beyond simple CRUD helpers) so the
scheduling can be unit-tested. The enforcer (enforce.py) turns the desired
state into Pi-hole group/client assignments.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime

log = logging.getLogger("famboard.control")

WEEKDAYS_IT = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MODES = ("block", "once", "schedule", "allow_window", "quota")
MAX_QUOTA_MINUTES = 24 * 60
_HM_RE = re.compile(r"(\d{1,2}):(\d{2})")


class RuleError(ValueError):
    """A rule (or one of its time windows) that cannot be interpreted."""


def parse_hm(s) -> int:
    """'HH:MM' as minutes since midnight. Raises RuleError on anything else:
    'aa:bb', '25:99', '7', None."""
    match = _HM_RE.fullmatch(s.strip()) if isinstance(s, str) else None
    if not match:
        raise RuleError(f"orario non valido: {s!r} (atteso HH:MM)")
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        raise RuleError(f"orario non valido: {s!r} (atteso HH:MM)")
    return hours * 60 + minutes


def _hm(s: str) -> int:
    return parse_hm(s)


def validate_window(win) -> dict:
    """Check one {"start","end","days"} window and return it normalised
    (zero-padded times, sorted unique days). Raises RuleError.

    start == end is refused: it describes an empty window, which for a
    'schedule' rule never blocks and for an 'allow_window' rule blocks the
    device around the clock. Neither is what the form was asked for.
    """
    if not isinstance(win, dict):
        raise RuleError("finestra oraria non valida")
    start, end = parse_hm(win.get("start")), parse_hm(win.get("end"))
    if start == end:
        raise RuleError("inizio e fine della finestra coincidono")
    out = {"start": f"{start // 60:02d}:{start % 60:02d}",
           "end": f"{end // 60:02d}:{end % 60:02d}"}
    days = win.get("days")
    if days is not None:
        if (not isinstance(days, (list, tuple)) or not days
                or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6
                       for d in days)):
            raise RuleError("giorni non validi (attesi numeri da 0=lun a 6=dom)")
        out["days"] = sorted(set(days))
    return out


def validate_rule(target: str, mode: str, from_ts: float | None = None,
                  until_ts: float | None = None, windows: list[dict] | None = None,
                  quota_minutes: int | None = None) -> list[dict] | None:
    """Check a rule before it is stored; return its windows, normalised.

    A rule that cannot be interpreted must never reach the table: the enforcer
    reads every enabled rule on every cycle.
    """
    from .taxonomy import CATEGORIES
    if mode not in MODES:
        raise RuleError(f"modalità sconosciuta: {mode!r}")
    if target != "all" and target not in CATEGORIES:
        raise RuleError(f"categoria sconosciuta: {target!r}")
    for name, value in (("from_ts", from_ts), ("until_ts", until_ts)):
        if value is not None and (isinstance(value, bool)
                                  or not isinstance(value, (int, float)) or value != value):
            raise RuleError(f"{name} non valido")
    if from_ts is not None and until_ts is not None and until_ts <= from_ts:
        raise RuleError("la fine del blocco precede l'inizio")
    if mode in ("schedule", "allow_window"):
        if not windows:
            raise RuleError("serve almeno una finestra oraria")
        return [validate_window(w) for w in windows]
    if mode == "quota":
        if (isinstance(quota_minutes, bool) or not isinstance(quota_minutes, int)
                or not 1 <= quota_minutes <= MAX_QUOTA_MINUTES):
            raise RuleError(f"minuti di quota non validi (1-{MAX_QUOTA_MINUTES})")
    return None


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


def blocking_now_or_none(rule, now_ts: float | None = None, conn=None) -> bool | None:
    """rule_blocking_now, or None when the stored rule cannot be interpreted
    (a window without 'start', a time like 'aa:bb', days that are not numbers).

    Such a row can only predate validation or come from a hand edit, but one of
    them used to raise out of desired_state and stop enforcement for every
    device, every cycle. It is now reported and skipped: that one rule does not
    block, all the others keep working.
    """
    try:
        return rule_blocking_now(rule, now_ts, conn=conn)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        log.warning("regola di controllo #%s non interpretabile, ignorata: %s",
                    rule["id"], exc)
        return None


def desired_state(conn: sqlite3.Connection, now_ts: float | None = None) -> dict[str, dict]:
    """Return {mac: {'all': bool, 'cats': set[str]}} for rules blocking right now."""
    from .devices import member_macs_for_mac
    out: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM control_rule WHERE enabled = 1").fetchall():
        if not blocking_now_or_none(r, now_ts, conn=conn):
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
    """Store a rule. Raises RuleError, storing nothing, if it is not valid."""
    windows = validate_rule(target, mode, from_ts, until_ts, windows, quota_minutes)
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
    try:
        return _describe(rule, conn, now_ts)
    except (ValueError, TypeError, KeyError, AttributeError):
        # Shown on the device page next to the delete button: a 500 there would
        # leave no way to remove the broken rule from the interface.
        return (f"{_target_label(rule['target'])} — ⚠️ regola non valida, "
                f"ignorata: eliminala e ricreala")


def _describe(rule, conn=None, now_ts: float | None = None) -> str:
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
