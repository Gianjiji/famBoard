"""Functional + SECURITY: web dashboard (FastAPI TestClient)."""
from famboard import db
from famboard.config import settings


# --- security: authentication -------------------------------------------
def test_protected_page_redirects_when_anonymous(anon_client):
    r = anon_client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_page_and_health_are_public(anon_client):
    assert anon_client.get("/login").status_code == 200
    assert anon_client.get("/health").status_code == 200


def test_wrong_password_does_not_authenticate(anon_client):
    anon_client.post("/login", data={"password": "WRONG"})
    assert anon_client.get("/").status_code == 303  # still blocked


def test_correct_login_grants_access(anon_client):
    anon_client.post("/login", data={"password": "testpass"})
    assert anon_client.get("/").status_code == 200


# --- functional: pages render -------------------------------------------
def test_main_pages_render(client):
    for path in ("/", "/people", "/devices", "/alerts", "/status",
                 "/?window=7d", "/alerts?scope=all"):
        assert client.get(path).status_code == 200, path


# --- security: no secret leakage ----------------------------------------
def test_dashboard_does_not_leak_password(client):
    assert "testpass" not in client.get("/").text


# --- functional + security: person CRUD, injection-safe -----------------
def test_person_crud_and_sql_injection_safe(client):
    malicious = "Robert'); DROP TABLE person;--"
    client.post("/people", data={"name": malicious, "kind": "minor"})
    # table must still exist and the name stored literally (parameterized SQL)
    conn = db.connect(settings.db_path)
    try:
        names = [r["name"] for r in conn.execute("SELECT name FROM person")]
    finally:
        conn.close()
    assert malicious in names
    assert client.get("/people").status_code == 200


def test_device_activity_log_with_category_filter(client):
    import time
    conn = db.connect(settings.db_path)
    conn.execute("INSERT INTO device(mac) VALUES('aa:bb:cc:dd:ee:50')")
    now = time.time()
    conn.executemany(
        "INSERT INTO query_event(ts, domain, mac, category_key, blocked) "
        "VALUES(?,?,?,?,?)",
        [(now - 60, "facebook.com", "aa:bb:cc:dd:ee:50", "social", 0),
         (now - 30, "pornsite.example", "aa:bb:cc:dd:ee:50", "adult", 1)])
    conn.commit()
    did = conn.execute("SELECT id FROM device WHERE mac='aa:bb:cc:dd:ee:50'").fetchone()["id"]
    conn.close()
    full = client.get(f"/device/{did}")
    assert full.status_code == 200
    assert "facebook.com" in full.text and "pornsite.example" in full.text
    filtered = client.get(f"/device/{did}?cat=adult")
    assert filtered.status_code == 200
    assert "pornsite.example" in filtered.text
    # the log table must not list the social-only domain when filtered
    log_part = filtered.text.split('id="log"')[1].split("</section>")[0]
    assert "facebook.com" not in log_part


def test_device_control_rule_flow(client):
    conn = db.connect(settings.db_path)
    conn.execute("INSERT INTO device(mac) VALUES('aa:bb:cc:dd:ee:40')")
    conn.commit()
    did = conn.execute("SELECT id FROM device WHERE mac='aa:bb:cc:dd:ee:40'").fetchone()["id"]
    conn.close()
    assert client.get(f"/device/{did}").status_code == 200
    # create a quota rule, then clear it
    client.post(f"/device/{did}/control/quota", data={"target": "social", "minutes": "60"})
    conn = db.connect(settings.db_path)
    n = conn.execute("SELECT COUNT(*) n FROM control_rule WHERE mac='aa:bb:cc:dd:ee:40'").fetchone()["n"]
    conn.close()
    assert n == 1
    client.post(f"/device/{did}/control/allow")


# --- functional: category drill-down ------------------------------------
def test_category_page_lists_devices_and_domains(client):
    import time
    conn = db.connect(settings.db_path)
    conn.execute("INSERT INTO device(mac, label) VALUES('aa:bb:cc:dd:ee:60','TabletTest')")
    conn.execute("INSERT INTO person(name, kind) VALUES('CatKid','minor')")
    pid = conn.execute("SELECT id FROM person WHERE name='CatKid'").fetchone()["id"]
    conn.execute("UPDATE device SET person_id=? WHERE mac='aa:bb:cc:dd:ee:60'", (pid,))
    now = time.time()
    conn.executemany(
        "INSERT INTO query_event(ts, domain, mac, category_key, blocked) "
        "VALUES(?,?,?,?,?)",
        [(now - 60, "facebook.com", "aa:bb:cc:dd:ee:60", "social", 0),
         (now - 30, "netflix.com", "aa:bb:cc:dd:ee:60", "entertainment", 0)])
    conn.commit()
    conn.close()

    r = client.get("/category/social")
    assert r.status_code == 200
    assert "facebook.com" in r.text and "TabletTest" in r.text
    assert "netflix.com" not in r.text  # other categories stay out

    # person filter: only that person's devices
    r = client.get(f"/category/social?pid={pid}")
    assert r.status_code == 200 and "facebook.com" in r.text

    # unknown category -> safe redirect home, no error page
    r = client.get("/category/nope", follow_redirects=False)
    assert r.status_code == 303

    # home page links to the category drill-down
    assert '/category/social' in client.get("/").text


# --- regression: non-ASCII password must not crash the login ------------
def test_login_with_non_ascii_password_fails_cleanly(anon_client):
    r = anon_client.post("/login", data={"password": "perché±”"})
    assert r.status_code == 303                      # no 500
    assert r.headers["location"] == "/login?error=1"
    assert anon_client.get("/").status_code == 303   # still not authenticated


# --- functional: attention page + force-report button ------------------
def test_attention_page_and_report_button(client):
    import time
    conn = db.connect(settings.db_path)
    conn.execute("INSERT INTO person(name,kind) VALUES('AttnKid','minor')")
    pid = conn.execute("SELECT id FROM person WHERE name='AttnKid'").fetchone()["id"]
    conn.execute("INSERT INTO device(mac,person_id) VALUES('aa:bb:cc:dd:ee:70',?)", (pid,))
    now = time.time()
    for i in range(50):
        conn.execute("INSERT INTO query_event(ts,mac,domain,category_key,blocked) "
                     "VALUES(?,?,?,?,0)",
                     (now - (i % 7) * 86400, "aa:bb:cc:dd:ee:70",
                      f"d{i%12}.adult.example", "adult"))
    conn.commit(); conn.close()

    r = client.get("/attention")
    assert r.status_code == 200 and "AttnKid" in r.text

    # force a report (no webhook configured in tests -> decided-to-send, no channel)
    r = client.post("/attention/report-now", follow_redirects=False)
    assert r.status_code == 303 and "/attention" in r.headers["location"]


# --- functional: kids blocklist panel on a minor's page ----------------
def test_minor_kids_blocklist_panel_and_save(client):
    from famboard import kidsblock
    conn = db.connect(settings.db_path)
    conn.execute("INSERT INTO person(name,kind) VALUES('KidPanel','minor')")
    pid = conn.execute("SELECT id FROM person WHERE name='KidPanel'").fetchone()["id"]
    conn.execute("INSERT INTO device(mac,person_id) VALUES('aa:bb:cc:dd:ee:80',?)", (pid,))
    conn.commit(); conn.close()

    page = client.get(f"/person/{pid}")
    assert page.status_code == 200 and "Blocklist per minori" in page.text

    r = client.post(f"/person/{pid}/kids", data={"lists": ["porn", "gambling"]},
                    follow_redirects=False)
    assert r.status_code == 303
    conn = db.connect(settings.db_path)
    assert kidsblock.selected(conn, pid) == {"porn", "gambling"}
    conn.close()

    # an adult must NOT show the panel
    conn = db.connect(settings.db_path)
    conn.execute("INSERT INTO person(name,kind) VALUES('AdultNoPanel','adult')")
    apid = conn.execute("SELECT id FROM person WHERE name='AdultNoPanel'").fetchone()["id"]
    conn.commit(); conn.close()
    assert "Blocklist per minori" not in client.get(f"/person/{apid}").text


# --- functional: attention PDF download --------------------------------
def test_attention_pdf_download(client):
    r = client.get("/attention/report.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.content[:4] == b"%PDF"
