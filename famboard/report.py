"""Reports delivered by webhook and/or email:
  - build/render: the routine weekly per-person activity report (timer-driven)
  - the event-driven *attention* report: sent only when the scored situation
    materially changes, plus an optional local-LLM context summary.
"""
from __future__ import annotations

import smtplib
import sqlite3
import time
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from . import analytics, attention, db
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
    _send_email(render_html(report), "famboard — report settimanale", result)
    return result


def _send_email(html: str, subject: str, result: dict,
                pdf: bytes | None = None, pdf_name: str = "report.pdf") -> None:
    """Send an HTML email, optionally with a PDF attachment."""
    if not (settings.smtp_host and settings.smtp_to):
        return
    try:
        if pdf is not None:
            msg = MIMEMultipart()
            msg.attach(MIMEText(html, "html", "utf-8"))
            part = MIMEApplication(pdf, _subtype="pdf")
            part.add_header("Content-Disposition", "attachment", filename=pdf_name)
            msg.attach(part)
        else:
            msg = MIMEText(html, "html", "utf-8")
        msg["Subject"] = subject
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


# ---------------------------------------------------------------------------
# Attention report (event-driven): who is worth a closer look, and why.
# ---------------------------------------------------------------------------
_LEVEL_LABEL = {"high": "ALTA", "concern": "da attenzionare", "watch": "sotto osservazione"}


def build_attention(conn: sqlite3.Connection, days: int = attention.WINDOW_DAYS,
                    now: float | None = None) -> dict:
    subjects = attention.score_subjects(conn, days=days, now=now)
    return {"days": days, "generated": now or time.time(),
            "subjects": subjects,
            "signature": attention.situation_signature(subjects)}


def _subject_line(s: dict) -> str:
    who = s["name"] + (" (minore)" if s.get("person_kind") == "minor"
                       else " (dispositivo)" if s["kind"] == "device" else "")
    return f"[{_LEVEL_LABEL.get(s['level'], s['level'])}] {who} — punteggio {s['score']}"


def render_attention_text(report: dict, flagged: list[dict] | None = None) -> str:
    subs = flagged if flagged is not None else report["subjects"]
    when = datetime.fromtimestamp(report["generated"]).strftime("%d/%m %H:%M")
    lines = [f"famboard — situazioni da attenzionare ({when}, ultimi {report['days']}gg)", ""]
    if not subs:
        lines.append("Nessuna situazione sopra soglia.")
        return "\n".join(lines)
    if report.get("ai_summary"):
        lines += ["🧠 " + report["ai_summary"], ""]
    for s in subs:
        lines.append("● " + _subject_line(s))
        for r in s["reasons"]:
            lines.append("   · " + r)
        owner = s["name"] if s["kind"] == "person" else "non assegnato"
        for d in s.get("devices", []):
            extra = " · ".join(v for v in (d.get("device_type"), owner) if v)
            lines.append(f"   · {d['label']} [{d['mac']}]" + (f" — {extra}" if extra else ""))
        lines.append("")
    lines.append("Il DNS rivela i domini contattati, non i contenuti: usa questo "
                 "come spunto per un confronto, non come prova.")
    return "\n".join(lines)


def render_attention_html(report: dict, flagged: list[dict] | None = None) -> str:
    subs = flagged if flagged is not None else report["subjects"]
    when = datetime.fromtimestamp(report["generated"]).strftime("%d/%m/%Y %H:%M")
    parts = [f"<h2>famboard — situazioni da attenzionare</h2>",
             f"<p style='color:#666'>{when} · ultimi {report['days']} giorni</p>"]
    if report.get("ai_summary"):
        parts.append(f"<p style='background:#f4f3ff;padding:10px;border-radius:8px'>"
                     f"🧠 {report['ai_summary']}</p>")
    if not subs:
        parts.append("<p>Nessuna situazione sopra soglia.</p>")
        return "".join(parts)
    colors = {"high": "#c0392b", "concern": "#e67e22", "watch": "#7f8c8d"}
    for s in subs:
        parts.append(f"<h3 style='color:{colors.get(s['level'], '#333')}'>{_subject_line(s)}</h3>")
        parts.append("<ul>" + "".join(f"<li>{r}</li>" for r in s["reasons"]) + "</ul>")
    parts.append("<p style='color:#888;font-size:12px'>Il DNS rivela i domini "
                 "contattati, non i contenuti: usalo come spunto per un confronto.</p>")
    return "".join(parts)


def ai_summary(report: dict) -> str | None:
    """Optional local-LLM context summary. Sends only the already-computed
    structured facts (names, categories, counts, reasons) to an OpenAI-compatible
    endpoint; never raw browsing content. Any failure returns None (non-fatal)."""
    if not settings.ai_summary_url or not report["subjects"]:
        return None
    facts = [{"soggetto": s["name"], "tipo": s["kind"], "livello": s["level"],
              "punteggio": s["score"], "motivi": s["reasons"]}
             for s in report["subjects"]]
    prompt = ("Sei un assistente che aiuta un genitore a leggere segnali di "
              "possibile disagio dai domini DNS contattati in casa (non dai "
              "contenuti). Dati questi fatti già calcolati, scrivi 2-3 frasi "
              "sobrie e non allarmistiche su cosa valga la pena osservare e come "
              "impostare un confronto sereno. Non inventare dettagli non presenti.\n\n"
              f"{facts}")
    try:
        headers = {"Content-Type": "application/json"}
        if settings.ai_summary_key:
            headers["Authorization"] = f"Bearer {settings.ai_summary_key}"
        r = httpx.post(
            settings.ai_summary_url.rstrip("/") + "/v1/chat/completions",
            headers=headers, timeout=60.0,
            json={"model": settings.ai_summary_model or "local",
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.3, "max_tokens": 220})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        print(f"[attention] AI summary skipped: {exc}")
        return None


def _deliver_attention(report: dict, flagged: list[dict]) -> dict:
    text = render_attention_text(report, flagged)
    result = {"webhook": False, "email": False}
    priority = "high" if any(s["level"] == "high" for s in flagged) else "default"
    if settings.webhook_url:
        try:
            httpx.post(settings.webhook_url, content=text.encode("utf-8"),
                       headers={"Title": "famboard - situazioni da attenzionare",
                                "Priority": priority}, timeout=10.0)
            result["webhook"] = True
        except Exception as exc:
            print(f"[attention] webhook error: {exc}")
    # email carries the full report as a styled PDF attachment
    pdf = None
    if settings.smtp_host and settings.smtp_to:
        try:
            from . import report_pdf
            pdf = report_pdf.render_attention_pdf(report, flagged)
        except Exception as exc:
            print(f"[attention] pdf render error: {exc}")
    fname = "famboard-attenzione-" + \
        datetime.fromtimestamp(report["generated"]).strftime("%Y%m%d-%H%M") + ".pdf"
    _send_email(render_attention_html(report, flagged),
                "famboard — situazioni da attenzionare", result,
                pdf=pdf, pdf_name=fname)
    return result


def dispatch_attention(conn: sqlite3.Connection, force: bool = False,
                       now: float | None = None) -> dict:
    """Event-driven send. Delivers only when the flagged situation has
    materially changed since the last send (signature differs) and a minimum
    quiet interval has elapsed — so repeat reports always carry an update,
    never a copy. `force=True` (dashboard button) bypasses both gates."""
    now = now if now is not None else time.time()
    report = build_attention(conn, now=now)
    flagged = [s for s in report["subjects"] if s["level"] in attention.AUTO_LEVELS] \
        if not force else report["subjects"]
    sig = attention.situation_signature(flagged)
    last_sig = db.get_meta(conn, "attention_sig", "")
    last_ts = float(db.get_meta(conn, "attention_sent_ts", "0") or 0)

    if not flagged:
        db.set_meta(conn, "attention_sig", "")  # situation cleared
        conn.commit()
        return {"sent": False, "reason": "nothing flagged", "flagged": 0}
    if not force:
        if sig == last_sig:
            return {"sent": False, "reason": "no change", "flagged": len(flagged)}
        if now - last_ts < settings.attention_min_interval:
            return {"sent": False, "reason": "throttled", "flagged": len(flagged)}

    report["ai_summary"] = ai_summary({**report, "subjects": flagged})
    delivered = _deliver_attention(report, flagged)
    db.set_meta(conn, "attention_sig", sig)
    db.set_meta(conn, "attention_sent_ts", str(now))
    conn.commit()
    return {"sent": True, "flagged": len(flagged), "delivery": delivered,
            "forced": force}
