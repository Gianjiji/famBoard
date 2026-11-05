"""Shared test setup. Env is configured BEFORE importing famboard so the frozen
Settings pick it up: an isolated temp DB, a known dashboard password, no poller,
and no Pi-hole password (nothing should ever touch the real Pi-hole in tests).
"""
import os
import tempfile

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="famboard-test-"), "test.db")
os.environ["DB_PATH"] = _TMPDB
os.environ["APP_PASSWORD"] = "testpass"
os.environ["FAMBOARD_DISABLE_POLLER"] = "1"
os.environ["PIHOLE_PASSWORD"] = ""
os.environ["WEBHOOK_URL"] = ""
os.environ["SMTP_HOST"] = ""

import pytest  # noqa: E402

from famboard import db, taxonomy  # noqa: E402
from famboard.config import settings  # noqa: E402


def _fresh_db(path):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass
    db.init_db(path)
    conn = db.connect(path)
    taxonomy.seed_categories(conn)
    taxonomy.seed_curated(conn)
    return conn


@pytest.fixture
def conn():
    """A fresh, isolated in-file DB connection (categories + domains seeded)."""
    path = os.path.join(tempfile.mkdtemp(prefix="famboard-unit-"), "u.db")
    c = _fresh_db(path)
    yield c
    c.close()


@pytest.fixture
def client():
    """FastAPI TestClient backed by the settings DB, logged in by default."""
    from fastapi.testclient import TestClient
    from famboard.web.app import app

    _fresh_db(str(settings.db_path)).close()
    with TestClient(app) as c:
        c.post("/login", data={"password": "testpass"})
        yield c


@pytest.fixture
def anon_client():
    """TestClient that is NOT logged in and does not follow redirects."""
    from fastapi.testclient import TestClient
    from famboard.web.app import app

    _fresh_db(str(settings.db_path)).close()
    with TestClient(app, follow_redirects=False) as c:
        yield c


class FakePihole:
    """In-memory stand-in for PiholeClient used by enforce/ingest tests."""

    def __init__(self):
        self.groups = [{"name": "Default", "id": 0}]
        self.clients = []
        self.regex = []
        self.restarts = 0
        self.net_devices = []

    def network_devices(self, max_devices=999, max_addresses=50):
        return list(self.net_devices)

    def delete_network_device(self, device_id):
        self.net_devices = [d for d in self.net_devices if d["id"] != device_id]

    def list_groups(self):
        return self.groups

    def create_group(self, name, comment="famboard"):
        self.groups.append({"name": name, "id": len(self.groups)})

    def list_clients(self):
        return list(self.clients)

    def add_client(self, client, groups, comment="famboard"):
        self.clients.append({"client": client, "groups": groups, "comment": comment})

    def update_client(self, client, groups, comment="famboard"):
        for x in self.clients:
            if x["client"] == client:
                x["groups"], x["comment"] = groups, comment

    def delete_client(self, client):
        self.clients = [x for x in self.clients if x["client"] != client]

    def list_regex_deny(self):
        return self.regex

    def add_regex_deny(self, domain, groups, comment="famboard"):
        self.regex.append({"domain": domain, "groups": groups})

    def restart_dns(self):
        self.restarts += 1


@pytest.fixture
def fake_pihole():
    return FakePihole()
