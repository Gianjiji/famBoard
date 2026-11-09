"""Security/privacy hardening: authenticated push, local-only AI, login throttle,
and an opt-in contract test against a real Pi-hole."""
import os
from types import SimpleNamespace

import pytest

from famboard import config, notify, report


# --- host_is_local --------------------------------------------------------
def test_host_is_local_classifies_addresses():
    assert config.host_is_local("http://127.0.0.1:11434")
    assert config.host_is_local("http://localhost:8080")
    assert config.host_is_local("http://192.168.1.50/v1")
    assert config.host_is_local("http://10.0.0.5")
    assert not config.host_is_local("https://api.openai.com")
    assert not config.host_is_local("http://8.8.8.8:1234")
    assert not config.host_is_local("")


# --- authenticated push ---------------------------------------------------
def test_push_sends_bearer_token_when_set(monkeypatch):
    sent = {}

    def fake_post(url, content=None, headers=None, timeout=None):
        sent["url"] = url; sent["headers"] = headers
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(notify, "httpx", SimpleNamespace(post=fake_post))
    monkeypatch.setattr(notify, "settings", SimpleNamespace(
        webhook_url="https://ntfy.example/fam", webhook_token="s3cr3t"))
    assert notify.push("ciao", "titolo", "high") is True
    assert sent["headers"]["Authorization"] == "Bearer s3cr3t"
    assert sent["headers"]["Title"] == "titolo"


def test_push_noop_without_webhook(monkeypatch):
    monkeypatch.setattr(notify, "settings", SimpleNamespace(webhook_url="", webhook_token=""))
    assert notify.push("x", "y") is False


# --- AI summary refuses a non-local endpoint ------------------------------
def test_ai_summary_blocks_remote_endpoint(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("httpx non deve essere chiamato per endpoint remoti")
    monkeypatch.setattr(report, "httpx", SimpleNamespace(post=boom))
    monkeypatch.setattr(report, "settings", SimpleNamespace(
        ai_summary_url="https://api.openai.com", ai_summary_allow_remote=False,
        ai_summary_key="", ai_summary_model=""))
    rep = {"subjects": [{"name": "X", "kind": "person", "level": "high",
                         "score": 9, "reasons": ["r"]}]}
    assert report.ai_summary(rep) is None


# --- login throttle -------------------------------------------------------
def test_login_throttles_after_repeated_failures(anon_client):
    last = None
    for _ in range(config.settings.login_max_attempts + 1):
        last = anon_client.post("/login", data={"password": "WRONG"},
                                follow_redirects=False)
    assert "error=throttled" in last.headers["location"]
    # even the correct password is refused while throttled
    r = anon_client.post("/login", data={"password": "testpass"},
                         follow_redirects=False)
    assert "error=throttled" in r.headers["location"]


# --- opt-in contract test against a live Pi-hole --------------------------
@pytest.mark.skipif(not os.getenv("FAMBOARD_PIHOLE_TEST"),
                    reason="imposta FAMBOARD_PIHOLE_TEST=1 (+ PIHOLE_URL/PIHOLE_PASSWORD) per il test di contratto")
def test_pihole_api_contract_readonly():
    """Legge (senza modificare) gli endpoint che l'enforcer usa, per cogliere
    derive dell'API Pi-hole v6 che FakePihole non vedrebbe."""
    from famboard.pihole import PiholeClient
    s = config.settings
    with PiholeClient(s.pihole_url, s.pihole_password, s.pihole_verify_tls) as c:
        assert isinstance(c.list_groups(), list)
        assert isinstance(c.list_clients(), list)
        assert isinstance(c.list_adlists(), list)
        assert isinstance(c.list_regex_deny(), list)
        assert isinstance(c.dns_hosts(), list)
