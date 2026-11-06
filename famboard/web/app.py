"""FastAPI dashboard: census people, assign devices, view categorized activity,
track minor-sensitive alerts. A background thread polls the Pi-hole so the
dashboard stays fresh; web requests and the poller each use their own SQLite
connection.
"""
from __future__ import annotations

import hmac
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import (analytics, attention, control, db, devices as devmod, ingest,
                kidsblock, report, taxonomy)
from ..config import settings
from ..pihole import PiholeClient

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["datetime"] = \
    lambda ts: datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "—"

_stop = threading.Event()
BUCKETS = {"24h": 3600, "7d": 21600, "30d": 86400}
PUBLIC_PATHS = ("/login", "/static", "/logout", "/health")


def _poller() -> None:
    conn = db.connect(settings.db_path)
    taxonomy.seed_categories(conn)
    client = PiholeClient(settings.pihole_url, settings.pihole_password,
                          verify_tls=settings.pihole_verify_tls)
    while not _stop.is_set():
        try:
            ingest.run_cycle(conn, client)
        except Exception as exc:
            print(f"[poller] error: {exc}")
        _stop.wait(settings.poll_interval)
    client.close()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    taxonomy.seed_categories(conn)
    conn.close()
    if not settings.app_password:
        print("[auth] APP_PASSWORD empty -> dashboard login DISABLED (LAN only)")
    # Tests (and setups that only want the web UI) can skip the Pi-hole poller.
    if not os.getenv("FAMBOARD_DISABLE_POLLER"):
        t = threading.Thread(target=_poller, daemon=True)
        t.start()
    yield
    _stop.set()


app = FastAPI(title="famboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    if settings.app_password and not request.session.get("auth"):
        if not request.url.path.startswith(PUBLIC_PATHS):
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# SessionMiddleware is added last so it sits OUTSIDE auth_guard and populates
# request.session before auth_guard reads it.
app.add_middleware(SessionMiddleware, secret_key=settings.resolve_secret(),
                   max_age=7 * 86400)


# -- helpers ---------------------------------------------------------------
def _ctx(request: Request, conn, nav: str, **extra) -> dict:
    ctx = {"request": request, "nav": nav}
    ctx.update(extra)
    return ctx


def _timeline(conn, since: float, window: str, macs=None) -> list[dict]:
    bucket = BUCKETS.get(window, 3600)
    rows = analytics.activity_timeline(conn, since, bucket, macs=macs)
    fmt = "%H:%M" if bucket < 86400 else "%d/%m"
    return [{"label": datetime.fromtimestamp(r["bucket"]).strftime(fmt),
             "total": r["total"], "blocked": r["blocked"]} for r in rows]


# -- auth ------------------------------------------------------------------
@app.get("/login")
def login_form(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html",
                                      {"request": request, "error": error})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    # compare bytes: compare_digest refuses non-ASCII str (typed accents/smart
    # quotes must fail auth cleanly, not crash with a 500)
    if settings.app_password and hmac.compare_digest(
            password.encode("utf-8"), settings.app_password.encode("utf-8")):
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/health")
def health():
    return {"ok": True}


# -- dashboard -------------------------------------------------------------
@app.get("/")
def index(request: Request, window: str = "24h"):
    conn = db.connect(settings.db_path)
    try:
        since = analytics.since_ts(window)
        ctx = _ctx(request, conn, "home",
                   window=window, windows=list(analytics.WINDOWS),
                   ov=analytics.overview(conn, since),
                   cats=analytics.category_breakdown(conn, since),
                   alerts=analytics.minor_alerts(conn, since),
                   critical=analytics.critical_alerts(conn, since),
                   unassigned=analytics.unassigned_sensitive(conn, since),
                   timeline=_timeline(conn, since, window),
                   hourly=analytics.hourly_distribution(conn, since),
                   topdom=analytics.top_domains(conn, since, limit=12))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/category/{key}")
def category_detail(request: Request, key: str, window: str = "24h", pid: int = 0):
    """Cross-device drill-down: who reached which domains of this category."""
    conn = db.connect(settings.db_path)
    try:
        cat = conn.execute("SELECT * FROM category WHERE key=?", (key,)).fetchone()
        if cat is None:
            return RedirectResponse("/", status_code=303)
        person = None
        macs = None
        if pid:
            row = conn.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
            if row is not None:
                person = dict(row)
                macs = analytics.device_macs_for_person(conn, pid)
        since = analytics.since_ts(window)
        ctx = _ctx(request, conn, "home",
                   window=window, windows=list(analytics.WINDOWS),
                   cat=dict(cat), person=person,
                   log=analytics.category_log(conn, since, key, macs=macs))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "category.html", ctx)


# -- people ----------------------------------------------------------------
@app.get("/people")
def people_page(request: Request):
    conn = db.connect(settings.db_path)
    try:
        ctx = _ctx(request, conn, "people", people=analytics.people(conn))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "people.html", ctx)


@app.post("/people")
def people_add(name: str = Form(...), kind: str = Form(...),
               birth_year: str = Form(""), note: str = Form("")):
    conn = db.connect(settings.db_path)
    try:
        by = int(birth_year) if birth_year.strip().isdigit() else None
        conn.execute("INSERT INTO person(name, kind, birth_year, note) VALUES(?,?,?,?)",
                     (name.strip(), kind, by, note.strip() or None))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/people", status_code=303)


@app.post("/people/{pid}/edit")
def people_edit(pid: int, name: str = Form(...), kind: str = Form(...),
                birth_year: str = Form(""), note: str = Form("")):
    conn = db.connect(settings.db_path)
    try:
        by = int(birth_year) if birth_year.strip().isdigit() else None
        conn.execute("UPDATE person SET name=?, kind=?, birth_year=?, note=? WHERE id=?",
                     (name.strip(), kind, by, note.strip() or None, pid))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/people", status_code=303)


@app.post("/people/{pid}/delete")
def people_delete(pid: int):
    conn = db.connect(settings.db_path)
    try:
        conn.execute("DELETE FROM person WHERE id=?", (pid,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/people", status_code=303)


# -- devices ---------------------------------------------------------------
@app.get("/devices")
def devices_page(request: Request):
    conn = db.connect(settings.db_path)
    try:
        devs = analytics.devices(conn)
        types = sorted({(d["device_type"] or "senza tipo") for d in devs})
        ctx = _ctx(request, conn, "devices", devices=devs,
                   people=analytics.people(conn), device_types=types,
                   suggestions=devmod.suggestions(conn))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "devices.html", ctx)


@app.post("/devices/{did}")
def device_update(did: int, label: str = Form(""), device_type: str = Form(""),
                  assignment: str = Form("")):
    conn = db.connect(settings.db_path)
    try:
        person_id = None
        is_shared = 0
        if assignment == "shared":
            is_shared = 1
        elif assignment.startswith("p"):
            person_id = int(assignment[1:])
        conn.execute(
            "UPDATE device SET label=?, device_type=?, person_id=?, is_shared=? WHERE id=?",
            (label.strip() or None, device_type.strip() or None, person_id, is_shared, did))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/devices", status_code=303)


@app.post("/device/{did}/merge")
def device_merge(did: int, child_id: int = Form(...)):
    conn = db.connect(settings.db_path)
    try:
        devmod.merge(conn, child_id, did)
    finally:
        conn.close()
    return RedirectResponse(f"/device/{did}", status_code=303)


@app.post("/device/{did}/unmerge")
def device_unmerge(did: int, member_id: int = Form(...)):
    conn = db.connect(settings.db_path)
    try:
        devmod.unmerge(conn, member_id)
    finally:
        conn.close()
    return RedirectResponse(f"/device/{did}", status_code=303)


@app.get("/device/{did}")
def device_detail(request: Request, did: int, window: str = "7d", cat: str = ""):
    conn = db.connect(settings.db_path)
    try:
        dev = conn.execute(
            "SELECT d.*, p.name person_name, p.kind person_kind FROM device d "
            "LEFT JOIN person p ON p.id = d.person_id WHERE d.id=?", (did,)).fetchone()
        if dev is None:
            return RedirectResponse("/devices", status_code=303)
        macs = devmod.member_macs(conn, dev["id"])
        since = analytics.since_ts(window)
        rules = []
        for m in macs:
            rules += control.rules_for(conn, m)
        for r in rules:
            r["desc"] = control.describe(r, conn=conn)
        enf = conn.execute("SELECT value FROM meta WHERE key='enforce'").fetchone()
        members = devmod.member_devices(conn, dev["id"])
        candidates = [d for d in analytics.devices(conn) if d["id"] != dev["id"]]
        ctx = _ctx(request, conn, "devices", window=window, windows=list(analytics.WINDOWS),
                   dev=dict(dev), members=members, candidates=candidates,
                   cats=analytics.category_breakdown(conn, since, macs=macs),
                   timeline=_timeline(conn, since, window, macs=macs),
                   hourly=analytics.hourly_distribution(conn, since, macs=macs),
                   topdom=analytics.top_domains(conn, since, macs=macs, limit=20),
                   log=analytics.recent_events(conn, since, macs=macs,
                                               cat_key=cat or None, limit=100),
                   logcat=cat,
                   rules=rules, categories=taxonomy.CATEGORIES,
                   enforce_on=bool(enf and enf["value"] == "1"))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "device.html", ctx)


# -- access control --------------------------------------------------------
def _device_mac(conn, did: int) -> str | None:
    r = conn.execute("SELECT mac FROM device WHERE id=?", (did,)).fetchone()
    return r["mac"] if r else None


def _until_from_time(hhmm: str) -> float | None:
    """Next occurrence of HH:MM (today if still ahead, else tomorrow)."""
    if not hhmm or ":" not in hhmm:
        return None
    h, m = (int(x) for x in hhmm.split(":"))
    now = datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


@app.post("/device/{did}/control/block")
def control_block(did: int, target: str = Form("all")):
    conn = db.connect(settings.db_path)
    try:
        mac = _device_mac(conn, did)
        if mac:
            control.add_rule(conn, mac, target, "block")
    finally:
        conn.close()
    return RedirectResponse(f"/device/{did}", status_code=303)


@app.post("/device/{did}/control/quota")
def control_quota(did: int, target: str = Form("all"), minutes: int = Form(60)):
    conn = db.connect(settings.db_path)
    try:
        mac = _device_mac(conn, did)
        if mac and minutes > 0:
            control.add_rule(conn, mac, target, "quota", quota_minutes=minutes)
    finally:
        conn.close()
    return RedirectResponse(f"/device/{did}", status_code=303)


@app.post("/device/{did}/control/timed")
def control_timed(did: int, target: str = Form("all"), minutes: int = Form(0),
                  delay: int = Form(0), until_time: str = Form("")):
    conn = db.connect(settings.db_path)
    try:
        mac = _device_mac(conn, did)
        if mac:
            now = time.time()
            from_ts = now + delay * 60 if delay else None
            base = from_ts if from_ts else now
            until_ts = None
            if until_time:
                until_ts = _until_from_time(until_time)
            elif minutes:
                until_ts = base + minutes * 60
            control.add_rule(conn, mac, target, "once", from_ts=from_ts, until_ts=until_ts)
    finally:
        conn.close()
    return RedirectResponse(f"/device/{did}", status_code=303)


@app.post("/device/{did}/control/schedule")
def control_schedule(did: int, target: str = Form("all"), kind: str = Form("schedule"),
                     days: list[str] = Form([]), start: str = Form(...), end: str = Form(...)):
    conn = db.connect(settings.db_path)
    try:
        mac = _device_mac(conn, did)
        if mac:
            day_ints = [int(d) for d in days] if days else None
            win = {"start": start, "end": end}
            if day_ints is not None:
                win["days"] = day_ints
            mode = "allow_window" if kind == "allow_window" else "schedule"
            control.add_rule(conn, mac, target, mode, windows=[win])
    finally:
        conn.close()
    return RedirectResponse(f"/device/{did}", status_code=303)


@app.post("/device/{did}/control/allow")
def control_allow(did: int):
    conn = db.connect(settings.db_path)
    try:
        mac = _device_mac(conn, did)
        if mac:
            conn.execute("DELETE FROM control_rule WHERE mac=?", (mac,))
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/device/{did}", status_code=303)


@app.post("/control/{rid}/delete")
def control_delete(rid: int, did: int = Form(...)):
    conn = db.connect(settings.db_path)
    try:
        control.delete_rule(conn, rid)
    finally:
        conn.close()
    return RedirectResponse(f"/device/{did}", status_code=303)


@app.post("/person/{pid}/control/block-all")
def person_block_all(pid: int):
    conn = db.connect(settings.db_path)
    try:
        for mac in analytics.device_macs_for_person(conn, pid):
            control.add_rule(conn, mac, "all", "block")
    finally:
        conn.close()
    return RedirectResponse(f"/person/{pid}", status_code=303)


@app.post("/person/{pid}/control/allow-all")
def person_allow_all(pid: int):
    conn = db.connect(settings.db_path)
    try:
        for mac in analytics.device_macs_for_person(conn, pid):
            conn.execute("DELETE FROM control_rule WHERE mac=?", (mac,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/person/{pid}", status_code=303)


@app.post("/status/enforce")
def toggle_enforce(enable: str = Form("")):
    conn = db.connect(settings.db_path)
    try:
        db.set_meta(conn, "enforce", "1" if enable == "1" else "0")
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/status", status_code=303)


# -- person detail ---------------------------------------------------------
@app.get("/person/{pid}")
def person_detail(request: Request, pid: int, window: str = "7d"):
    conn = db.connect(settings.db_path)
    try:
        person = conn.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
        if person is None:
            return RedirectResponse("/people", status_code=303)
        macs = analytics.device_macs_for_person(conn, pid)
        since = analytics.since_ts(window)
        cats = analytics.category_breakdown(conn, since, macs=macs)
        sensitive = [c for c in cats if c["minor_sensitive"]]
        samples = {c["key"]: analytics.recent_by_category(conn, since, c["key"], macs=macs, limit=15)
                   for c in sensitive}
        ctx = _ctx(request, conn, "people", window=window, windows=list(analytics.WINDOWS),
                   person=dict(person),
                   devices=[dict(r) for r in conn.execute(
                       "SELECT * FROM device WHERE person_id=? ORDER BY last_seen DESC",
                       (pid,)).fetchall()],
                   cats=cats, sensitive=sensitive, samples=samples,
                   timeline=_timeline(conn, since, window, macs=macs),
                   hourly=analytics.hourly_distribution(conn, since, macs=macs),
                   topdom=analytics.top_domains(conn, since, macs=macs, limit=15))
        if person["kind"] == "minor":
            ctx["kids_catalog"] = kidsblock.catalog()
            ctx["kids_selected"] = kidsblock.selected(conn, pid)
            enf = conn.execute("SELECT value FROM meta WHERE key='enforce'").fetchone()
            ctx["enforce_on"] = bool(enf and enf["value"] == "1")
    finally:
        conn.close()
    return templates.TemplateResponse(request, "person.html", ctx)


@app.post("/person/{pid}/kids")
def person_kids(pid: int, lists: list[str] = Form(default=[])):
    conn = db.connect(settings.db_path)
    try:
        kidsblock.set_selection(conn, pid, lists)
    finally:
        conn.close()
    return RedirectResponse(f"/person/{pid}#kids", status_code=303)


# -- attention -------------------------------------------------------------
@app.get("/attention")
def attention_page(request: Request, days: int = attention.WINDOW_DAYS):
    conn = db.connect(settings.db_path)
    try:
        subjects = attention.score_subjects(conn, days=days)
        last_ts = db.get_meta(conn, "attention_sent_ts", "")
        ctx = _ctx(request, conn, "attention", subjects=subjects, days=days,
                   auto_levels=attention.AUTO_LEVELS,
                   last_sent=float(last_ts) if last_ts else None)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "attention.html", ctx)


@app.get("/attention/report.pdf")
def attention_report_pdf():
    from .. import report_pdf
    conn = db.connect(settings.db_path)
    try:
        rep = report.build_attention(conn)
    finally:
        conn.close()
    pdf = report_pdf.render_attention_pdf(rep)
    fname = f"famboard-attenzione-{datetime.now().strftime('%Y%m%d-%H%M')}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# -- status ----------------------------------------------------------------
@app.get("/status")
def status_page(request: Request):
    conn = db.connect(settings.db_path)
    pihole_ok = False
    pihole_info = ""
    try:
        with PiholeClient(settings.pihole_url, settings.pihole_password,
                          verify_tls=settings.pihole_verify_tls) as c:
            s = c.stats_summary()
            pihole_ok = True
            q = s.get("queries", {})
            pihole_info = f"{q.get('total','?')} query totali, {q.get('percent_blocked',0):.1f}% bloccate"
    except Exception as exc:
        pihole_info = str(exc)[:200]
    try:
        cov = analytics.coverage(conn)
        cov["last_age"] = (time.time() - cov["last_ts"]) if cov["last_ts"] else None
        enf = conn.execute("SELECT value FROM meta WHERE key='enforce'").fetchone()
        ctx = _ctx(request, conn, "status", cov=cov, pihole_ok=pihole_ok,
                   pihole_info=pihole_info, pihole_url=settings.pihole_url,
                   poll_interval=settings.poll_interval,
                   webhook_set=bool(settings.webhook_url),
                   auth_on=bool(settings.app_password),
                   enforce_on=bool(enf and enf["value"] == "1"))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "status.html", ctx)
