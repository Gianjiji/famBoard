"""Rule validation and resilience.

A rule nobody can interpret must never be stored, and one that is already in
the table (from before validation, or a hand edit) must not take enforcement
down for every other device. Web forms answer 400, never 500.
"""
import json
import logging
import time
from datetime import datetime

import pytest

from famboard import control, db, enforce
from famboard.config import settings

MAC = "aa:bb:cc:dd:ee:01"
OTHER = "aa:bb:cc:dd:ee:02"


def at(hhmm: str, day: int = 2) -> float:
    """Epoch for June `day` 2026 (a Tuesday on the 2nd) at local HH:MM."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 6, day, h, m).timestamp()


def rule_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM control_rule").fetchone()[0]


# --- parse_hm ---------------------------------------------------------------
@pytest.mark.parametrize("text, minutes", [
    ("00:00", 0), ("07:05", 425), ("7:05", 425), ("23:59", 1439), (" 21:30 ", 1290),
])
def test_parse_hm_accepts_times_of_day(text, minutes):
    assert control.parse_hm(text) == minutes


@pytest.mark.parametrize("text", [
    "aa:bb", "25:99", "24:00", "12:60", "7", "", ":", "12:", ":30", "12:3", "1:2:3",
    "-1:30", "12.30", "１２:３０x", None, 730, ["07:30"], "07:30; DROP TABLE control_rule",
])
def test_parse_hm_refuses_everything_else(text):
    with pytest.raises(control.RuleError):
        control.parse_hm(text)


# --- validate_window --------------------------------------------------------
def test_window_is_normalised():
    assert control.validate_window({"start": "7:05", "end": "21:00", "days": [4, 0, 4]}) == \
        {"start": "07:05", "end": "21:00", "days": [0, 4]}
    assert control.validate_window({"start": "22:00", "end": "07:00"}) == \
        {"start": "22:00", "end": "07:00"}          # overnight, every day


@pytest.mark.parametrize("win", [
    {}, {"start": "aa", "end": "bb"}, {"start": "07:00"}, {"end": "07:00"},
    {"start": "25:99", "end": "07:00"}, {"start": "07:00", "end": "07:00"},
    {"start": "07:00", "end": "08:00", "days": []},
    {"start": "07:00", "end": "08:00", "days": [7]},
    {"start": "07:00", "end": "08:00", "days": [-1]},
    {"start": "07:00", "end": "08:00", "days": ["1"]},
    {"start": "07:00", "end": "08:00", "days": [True]},
    {"start": "07:00", "end": "08:00", "days": "0123"},
    None, "07:00-08:00", ["07:00", "08:00"],
])
def test_window_refused(win):
    with pytest.raises(control.RuleError):
        control.validate_window(win)


# --- add_rule ---------------------------------------------------------------
@pytest.mark.parametrize("kwargs", [
    dict(target="all", mode="schedule", windows=[{"start": "aa", "end": "bb"}]),
    dict(target="all", mode="schedule", windows=[]),
    dict(target="all", mode="schedule", windows=None),
    dict(target="all", mode="allow_window", windows=[{"start": "25:99", "end": "07:00"}]),
    dict(target="all", mode="allow_window", windows=[{"start": "09:00", "end": "09:00"}]),
    dict(target="all", mode="schedule",
         windows=[{"start": "08:00", "end": "09:00"}, {"start": "x", "end": "y"}]),
    dict(target="all", mode="sempre"),
    dict(target="all", mode=""),
    dict(target="categoria-inventata", mode="block"),
    dict(target="", mode="block"),
    dict(target="all", mode="quota"),
    dict(target="all", mode="quota", quota_minutes=0),
    dict(target="all", mode="quota", quota_minutes=-5),
    dict(target="all", mode="quota", quota_minutes=100000),
    dict(target="all", mode="quota", quota_minutes="60"),
    dict(target="all", mode="once", from_ts=2000.0, until_ts=1000.0),
    dict(target="all", mode="once", from_ts=1000.0, until_ts=1000.0),
    dict(target="all", mode="once", until_ts=float("nan")),
    dict(target="all", mode="once", until_ts="domani"),
])
def test_invalid_rule_is_refused_and_nothing_is_stored(conn, kwargs):
    with pytest.raises(control.RuleError):
        control.add_rule(conn, MAC, **kwargs)
    assert rule_count(conn) == 0


def test_valid_rules_are_stored_normalised(conn):
    rid = control.add_rule(conn, MAC, "all", "schedule",
                           windows=[{"start": "7:00", "end": "9:30", "days": [2, 1]}])
    row = conn.execute("SELECT windows FROM control_rule WHERE id=?", (rid,)).fetchone()
    assert json.loads(row["windows"]) == [{"start": "07:00", "end": "09:30", "days": [1, 2]}]
    control.add_rule(conn, MAC, "games", "block")
    control.add_rule(conn, MAC, "all", "quota", quota_minutes=60)
    control.add_rule(conn, MAC, "all", "once", until_ts=time.time() + 60)
    control.add_rule(conn, MAC, "all", "once")
    assert rule_count(conn) == 5


def test_rule_error_is_a_value_error():
    """Callers that already catch ValueError keep working."""
    assert issubclass(control.RuleError, ValueError)


# --- a broken stored rule does not stop the others --------------------------
def insert_raw(conn, mac, mode, windows, target="all"):
    """Bypass add_rule: what a database from before validation may contain."""
    cur = conn.execute(
        "INSERT INTO control_rule(mac, target, mode, windows) VALUES(?,?,?,?)",
        (mac, target, mode, windows))
    conn.commit()
    return cur.lastrowid


BROKEN_WINDOWS = [
    json.dumps([{"start": "aa", "end": "bb"}]),
    json.dumps([{"start": "25:99", "end": "07:00"}]),
    json.dumps([{"end": "07:00"}]),
    json.dumps([{"start": "07:00", "end": "08:00", "days": "lun"}]),
    json.dumps(["07:00-08:00"]),
    json.dumps({"start": "07:00", "end": "08:00"}),
    json.dumps([None]),
    json.dumps(42),
]


@pytest.mark.parametrize("windows", BROKEN_WINDOWS)
@pytest.mark.parametrize("mode", ["schedule", "allow_window"])
def test_broken_rule_is_skipped_not_fatal(conn, caplog, mode, windows):
    broken = insert_raw(conn, MAC, mode, windows)
    control.add_rule(conn, OTHER, "all", "block")
    with caplog.at_level(logging.WARNING, logger="famboard.control"):
        state = control.desired_state(conn, at("12:00"))
    assert state == {OTHER: {"all": True, "cats": set()}}
    assert f"#{broken}" in caplog.text


def test_broken_rule_does_not_stop_enforcement(conn, fake_pihole):
    """The whole reconcile cycle, not just the pure function."""
    insert_raw(conn, MAC, "schedule", json.dumps([{"start": "aa", "end": "bb"}]))
    control.add_rule(conn, OTHER, "all", "block")
    conn.executemany("INSERT INTO ip_map(ip, mac) VALUES(?, ?)",
                     [("192.168.1.10", MAC), ("192.168.1.20", OTHER)])
    conn.commit()
    enforce.reconcile(conn, fake_pihole)          # used to raise ValueError here
    clients = {c["client"]: c["groups"] for c in fake_pihole.clients}   # keyed by current IP
    assert clients.get("192.168.1.20", [0]) != [0], "the valid rule must still block"
    assert "192.168.1.10" not in clients, "the broken rule blocks nothing"


def test_valid_rule_on_the_same_device_still_applies(conn):
    insert_raw(conn, MAC, "schedule", json.dumps([{"start": "aa", "end": "bb"}]))
    control.add_rule(conn, MAC, "games", "block")
    assert control.desired_state(conn, at("12:00")) == {MAC: {"all": False, "cats": {"games"}}}


def test_describe_never_raises_on_a_broken_rule(conn):
    for windows in BROKEN_WINDOWS:
        insert_raw(conn, MAC, "schedule", windows)
    for rule in control.rules_for(conn, MAC):
        text = control.describe(rule, conn=conn, now_ts=at("12:00"))
        assert isinstance(text, str) and text


def test_describe_flags_the_broken_rule(conn):
    insert_raw(conn, MAC, "allow_window", json.dumps([{"start": "aa", "end": "bb"}]))
    assert "non valida" in control.describe(control.rules_for(conn, MAC)[0], conn=conn)


def test_schedule_semantics_unchanged(conn):
    control.add_rule(conn, MAC, "all", "schedule",
                     windows=[{"start": "22:00", "end": "07:00", "days": [1]}])   # Tuesday night
    blocked = lambda ts: MAC in control.desired_state(conn, ts)
    assert not blocked(at("21:59", day=2))
    assert blocked(at("22:00", day=2))
    assert blocked(at("06:59", day=3))         # Wednesday morning: belongs to Tuesday night
    assert not blocked(at("07:00", day=3))
    assert not blocked(at("23:00", day=3))     # Wednesday night is not in the rule


# --- web forms: 400, never 500 ----------------------------------------------
def device_id(mac=MAC):
    conn = db.connect(settings.db_path)
    try:
        cur = conn.execute("INSERT INTO device(mac) VALUES(?)", (mac,))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def stored_rules():
    conn = db.connect(settings.db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM control_rule")]
    finally:
        conn.close()


@pytest.mark.parametrize("data", [
    {"start": "aa", "end": "bb"},
    {"start": "25:99", "end": "07:00"},
    {"start": "", "end": ""},
    {"start": "08:00", "end": "08:00"},
    {"start": "08:00", "end": "09:00", "days": ["x"]},
    {"start": "08:00", "end": "09:00", "days": ["9"]},
    {"start": "08:00", "end": "09:00", "days": ["1", "-1"]},
    {"start": "08:00", "end": "09:00", "kind": "qualcosa"},
    {"start": "08:00", "end": "09:00", "target": "inventata"},
])
def test_schedule_form_with_bad_values_is_400(client, data):
    did = device_id()
    r = client.post(f"/device/{did}/control/schedule", data=data, follow_redirects=False)
    assert r.status_code in (400, 422), r.text[:200]   # 422: FastAPI on an empty required field
    assert stored_rules() == []


def test_schedule_form_with_good_values_stores_the_rule(client):
    did = device_id()
    r = client.post(f"/device/{did}/control/schedule",
                    data={"start": "21:30", "end": "07:00", "days": ["0", "1"],
                          "kind": "allow_window", "target": "all"},
                    follow_redirects=False)
    assert r.status_code == 303
    rules = stored_rules()
    assert len(rules) == 1 and rules[0]["mode"] == "allow_window"
    assert json.loads(rules[0]["windows"]) == [{"start": "21:30", "end": "07:00", "days": [0, 1]}]
    assert client.get(f"/device/{did}").status_code == 200


@pytest.mark.parametrize("data", [
    {"until_time": "aa:bb"}, {"until_time": "25:99"}, {"until_time": "7"},
    {"minutes": "-10"}, {"delay": "-1"}, {"minutes": "99999999"},
    {"minutes": "30", "target": "inventata"},
])
def test_timed_form_with_bad_values_is_400(client, data):
    did = device_id()
    r = client.post(f"/device/{did}/control/timed", data=data, follow_redirects=False)
    assert r.status_code == 400, r.text[:200]
    assert stored_rules() == []


def test_timed_form_with_good_values(client):
    did = device_id()
    for data in ({"minutes": "30"}, {"until_time": "23:59"}, {"delay": "5", "minutes": "20"}, {}):
        r = client.post(f"/device/{did}/control/timed", data=data, follow_redirects=False)
        assert r.status_code == 303, data
    assert len(stored_rules()) == 4


@pytest.mark.parametrize("path, data", [
    ("block", {"target": "inventata"}),
    ("quota", {"minutes": "100000"}),
    ("quota", {"target": "inventata", "minutes": "30"}),
])
def test_other_control_forms_with_bad_values_are_400(client, path, data):
    did = device_id()
    r = client.post(f"/device/{did}/control/{path}", data=data, follow_redirects=False)
    assert r.status_code == 400
    assert stored_rules() == []


def test_device_page_renders_with_a_broken_stored_rule(client):
    """Otherwise the page with the delete button could not be opened."""
    did = device_id()
    conn = db.connect(settings.db_path)
    insert_raw(conn, MAC, "schedule", json.dumps([{"start": "aa", "end": "bb"}]))
    conn.close()
    r = client.get(f"/device/{did}")
    assert r.status_code == 200
    assert "non valida" in r.text


@pytest.mark.parametrize("data", [
    {"name": "Ada", "kind": "robot"}, {"name": "Ada", "kind": ""},
    {"name": "   ", "kind": "adult"},
])
def test_person_form_with_bad_values_is_400(client, data):
    assert client.post("/people", data=data, follow_redirects=False).status_code in (400, 422)
    conn = db.connect(settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 0
    finally:
        conn.close()


def test_person_edit_with_bad_kind_is_400_and_changes_nothing(client):
    client.post("/people", data={"name": "Ada", "kind": "minor", "birth_year": "2014"})
    r = client.post("/people/1/edit", data={"name": "Ada", "kind": "robot"},
                    follow_redirects=False)
    assert r.status_code == 400
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute("SELECT kind, birth_year FROM person WHERE id=1").fetchone()
    finally:
        conn.close()
    assert (row["kind"], row["birth_year"]) == ("minor", 2014)


@pytest.mark.parametrize("assignment", ["pabc", "p", "p-1", "p1.5", "p999", "qualcosa", "p١"])
def test_device_assignment_with_bad_values_is_400(client, assignment):
    did = device_id()
    r = client.post(f"/devices/{did}", data={"assignment": assignment}, follow_redirects=False)
    assert r.status_code == 400


def test_device_assignment_good_values(client):
    did = device_id()
    client.post("/people", data={"name": "Ada", "kind": "minor"})
    for assignment, expected in (("p1", (1, 0)), ("shared", (None, 1)), ("", (None, 0))):
        r = client.post(f"/devices/{did}", data={"assignment": assignment, "label": "Tablet"},
                        follow_redirects=False)
        assert r.status_code == 303
        conn = db.connect(settings.db_path)
        try:
            row = conn.execute("SELECT person_id, is_shared FROM device WHERE id=?", (did,)).fetchone()
        finally:
            conn.close()
        assert (row["person_id"], row["is_shared"]) == expected
