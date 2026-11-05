"""Weekly per-person activity report, delivered by webhook and/or email."""
from __future__ import annotations

import smtplib
import sqlite3
import time
from datetime import datetime
from email.mime.text import MIMEText

import httpx

from . import analytics
from .config import settings

NIGHT_HOURS = set(range(22, 24)) | set(range(0, 7))


def _person_totals(conn: sqlite3.Connection, since: float, macs: list[str]) -> tuple[int, int]:
    if not macs:
        return (0, 0)
    ph = ",".join("?" * len(macs))
    r = conn.execute(
        f"SELECT COUNT(*) t, COALESCE(SUM(blocked),0) b FROM query_event "
        f"WHERE ts>=? AND mac IN ({ph})", [since] + macs).fetchone()
    return (r["t"], r["b"])


def build(conn: sqlite3.Connection, days: int = 7) -> dict:
    since = time.time() - days * 86400
    report = {"days": days, "since": since, "generated": time.time(), "people": []}
    for p in analytics.people(conn):
        if not p["n_devices"]:
            continue
        macs = analytics.device_macs_for_person(conn, p["id"])
        total, blocked = _person_totals(conn, since, macs)
        cats = analytics.category_breakdown(conn, since, macs=macs)
        hourly = analytics.hourly_distribution(conn, since, macs=macs)
        report["people"].append({
            "id": p["id"], "name": p["name"], "kind": p["kind"],
            "total": total, "blocked": blocked,
            "categories": cats[:6],
            "sensitive": [c for c in cats if c["minor_sensitive"]],
            "night": sum(hourly[h] for h in NIGHT_HOURS),
            "top": analytics.top_domains(conn, since, macs=macs, limit=5),
        })
    return report


def render_text(report: dict) -> str:
    start = datetime.fromtimestamp(report["since"]).strftime("%d/%m")
    end = datetime.fromtimestamp(report["generated"]).strftime("%d/%m")
    lines = [f"famboard — report {report['days']}gg ({start}–{end})", ""]
    if not report["people"]:
        lines.append("Nessuna persona con dispositivi assegnati.")
        return "\n".join(lines)
    for p in report["people"]:
        tag = "minore" if p["kind"] == "minor" else "adulto"
        lines.append(f"● {p['name']} ({tag}) — {p['total']} richieste, {p['blocked']} bloccate")
        if p["categories"]:
            cats = ", ".join(f"{c['label']} {c['n']}" for c in p["categories"])
            lines.append(f"  Categorie: {cats}")
        if p["sensitive"]:
            sens = ", ".join(f"{c['label']} {c['n']}" for c in p["sensitive"])
            lines.append(f"  ⚠️ Sensibili raggiunte: {sens}")
        if p["night"]:
            lines.append(f"  🌙 Attività notturna (22–07): {p['night']} richieste")
        lines.append("")
    return "\n".join(lines)


def render_html(report: dict) -> str:
    start = datetime.fromtimestamp(report["since"]).strftime("%d/%m/%Y")
    end = datetime.fromtimestamp(report["generated"]).strftime("%d/%m/%Y")
    parts = [f"<h2>famboard — report {report['days']} giorni</h2>",
             f"<p style='color:#666'>{start} – {end}</p>"]
    if not report["people"]:
        parts.append("<p>Nessuna persona con dispositivi assegnati.</p>")
        return "".join(parts)
    for p in report["people"]:
        tag = "minore" if p["kind"] == "minor" else "adulto"
        parts.append(f"<h3>{p['name']} <small style='color:#999'>({tag})</small></h3>")
        parts.append(f"<p>{p['total']} richieste · {p['blocked']} bloccate · "
                     f"🌙 {p['night']} notturne (22–07)</p>")
        if p["sensitive"]:
            sens = ", ".join(f"{c['label']} ({c['n']})" for c in p["sensitive"])
            parts.append(f"<p style='color:#c0392b'><b>⚠️ Categorie sensibili raggiunte:</b> {sens}</p>")
        if p["categories"]:
            rows = "".join(f"<tr><td>{c['label']}</td><td align='right'>{c['n']}</td></tr>"
                           for c in p["categories"])
            parts.append(f"<table cellpadding='4'>{rows}</table>")
    return "".join(parts)


def deliver(report: dict) -> dict:
    text = render_text(report)
    result = {"webhook": False, "email": False}
    if settings.webhook_url:
        try:
            httpx.post(settings.webhook_url, content=text.encode("utf-8"),
                       headers={"Title": "famboard - report settimanale"}, timeout=10.0)
            result["webhook"] = True
        except Exception as exc:
            print(f"[report] webhook error: {exc}")
    if settings.smtp_host and settings.smtp_to:
        try:
            msg = MIMEText(render_html(report), "html", "utf-8")
            msg["Subject"] = "famboard — report settimanale"
            msg["From"] = settings.smtp_from or settings.smtp_user
            msg["To"] = settings.smtp_to
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
                if settings.smtp_tls:
                    s.starttls()
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_pass)
                s.send_message(msg)
            result["email"] = True
        except Exception as exc:
            print(f"[report] smtp error: {exc}")
    return result
