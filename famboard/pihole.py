"""Minimal client for the Pi-hole v6 REST API.

Handles session auth (password -> sid), transparent re-auth on expiry/401,
and the two endpoints we rely on: the query log and the network device
inventory. Sessions are a limited resource on the Pi-hole, so this client
reuses one session and logs out on close().
"""
from __future__ import annotations

import time
from typing import Iterator
from urllib.parse import quote

import httpx


class PiholeError(RuntimeError):
    pass


class PiholeAuthError(PiholeError):
    pass


class PiholeClient:
    def __init__(self, base_url: str, password: str, verify_tls: bool = False,
                 timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._password = password
        self._client = httpx.Client(base_url=self.base_url, verify=verify_tls,
                                     timeout=timeout)
        self._sid: str | None = None
        self._csrf: str | None = None
        self._expiry: float = 0.0

    # -- session -----------------------------------------------------------
    def _login(self) -> None:
        if not self._password:
            raise PiholeAuthError("PIHOLE_PASSWORD is empty")
        r = self._client.post("/api/auth", json={"password": self._password})
        if r.status_code != 200:
            raise PiholeAuthError(f"auth failed: HTTP {r.status_code}")
        sess = (r.json() or {}).get("session", {})
        if not sess.get("valid"):
            raise PiholeAuthError("auth failed: invalid credentials")
        self._sid = sess["sid"]
        self._csrf = sess.get("csrf")
        self._expiry = time.time() + float(sess.get("validity", 1800))

    def _ensure_session(self) -> None:
        if self._sid and time.time() < self._expiry - 30:
            return
        self._login()

    def _request(self, method: str, path: str, **kw) -> dict:
        self._ensure_session()
        headers = {"X-FTL-SID": self._sid or ""}
        if method.upper() != "GET" and self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        r = self._client.request(method, path, headers=headers, **kw)
        if r.status_code == 401:  # session died early; re-auth once
            self._login()
            headers["X-FTL-SID"] = self._sid or ""
            r = self._client.request(method, path, headers=headers, **kw)
        if r.status_code >= 400:
            raise PiholeError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    # -- endpoints ---------------------------------------------------------
    def network_devices(self, max_devices: int = 999, max_addresses: int = 50) -> list[dict]:
        data = self._request("GET", "/api/network/devices",
                              params={"max_devices": max_devices,
                                      "max_addresses": max_addresses})
        return data.get("devices", [])

    def delete_network_device(self, device_id: int) -> dict:
        """Forget a device in the Pi-hole network table. Without this, pruning
        it from famboard is useless: the poller re-imports it on the next cycle."""
        return self._request("DELETE", f"/api/network/devices/{device_id}")

    # local DNS records ("IP hostname" entries in dns.hosts)
    def dns_hosts(self) -> list[str]:
        data = self._request("GET", "/api/config/dns/hosts")
        return data.get("config", {}).get("dns", {}).get("hosts", [])

    def add_dns_host(self, ip: str, hostname: str) -> dict:
        return self._request("PUT", f"/api/config/dns/hosts/{quote(f'{ip} {hostname}')}")

    def delete_dns_host(self, entry: str) -> dict:
        """entry is the exact stored string, e.g. '192.168.1.50 famboard.home'."""
        return self._request("DELETE", f"/api/config/dns/hosts/{quote(entry)}")

    def queries(self, since_ts: float, until_ts: float | None = None,
                page_size: int = 10000, max_rows: int = 500000) -> Iterator[dict]:
        """Yield query records (newest first) in the time window.

        Pages via the response cursor only when a full page is returned, so a
        normal poll cycle is a single request. Dedup is the caller's job.
        """
        collected = 0
        cursor: int | None = None
        while collected < max_rows:
            params: dict = {"from": int(since_ts), "length": page_size}
            if until_ts is not None:
                params["until"] = int(until_ts)
            if cursor is not None:
                params["cursor"] = cursor
            data = self._request("GET", "/api/queries", params=params)
            rows = data.get("queries") or []
            if not rows:
                break
            for row in rows:
                yield row
                collected += 1
            nxt = data.get("cursor")
            if len(rows) < page_size or not nxt or nxt == cursor:
                break
            cursor = nxt

    def stats_summary(self) -> dict:
        return self._request("GET", "/api/stats/summary")

    # -- group / client / domain management (for access control) ----------
    def list_groups(self) -> list[dict]:
        return self._request("GET", "/api/groups").get("groups", [])

    def create_group(self, name: str, comment: str = "famboard") -> dict:
        return self._request("POST", "/api/groups",
                             json={"name": name, "comment": comment})

    def list_clients(self) -> list[dict]:
        return self._request("GET", "/api/clients").get("clients", [])

    def add_client(self, client: str, groups: list[int], comment: str = "famboard") -> dict:
        return self._request("POST", "/api/clients",
                             json={"client": client, "comment": comment, "groups": groups})

    def update_client(self, client: str, groups: list[int], comment: str = "famboard") -> dict:
        from urllib.parse import quote
        return self._request("PUT", f"/api/clients/{quote(client, safe='')}",
                             json={"comment": comment, "groups": groups})

    def delete_client(self, client: str) -> dict:
        from urllib.parse import quote
        return self._request("DELETE", f"/api/clients/{quote(client, safe='')}")

    def list_regex_deny(self) -> list[dict]:
        return self._request("GET", "/api/domains/deny/regex").get("domains", [])

    def add_regex_deny(self, domain: str, groups: list[int], comment: str = "famboard") -> dict:
        return self._request("POST", "/api/domains/deny/regex",
                             json={"domain": domain, "comment": comment, "groups": groups})

    def delete_group(self, name: str) -> dict:
        from urllib.parse import quote
        return self._request("DELETE", f"/api/groups/{quote(name, safe='')}")

    def delete_regex_deny(self, domain: str) -> dict:
        from urllib.parse import quote
        return self._request("DELETE", f"/api/domains/deny/regex/{quote(domain, safe='')}")

    def restart_dns(self) -> dict:
        """Restart FTL: reloads group/client assignments AND flushes the DNS
        cache, so a new block (or unblock) takes effect immediately."""
        return self._request("POST", "/api/action/restartdns")

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        try:
            if self._sid:
                self._client.request("DELETE", "/api/auth",
                                     headers={"X-FTL-SID": self._sid})
        except Exception:
            pass
        finally:
            self._sid = None
            self._client.close()

    def __enter__(self) -> "PiholeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
