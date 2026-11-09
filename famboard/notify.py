"""Single place that sends a push notification to the configured webhook.

Pushes carry sensitive content (minors' names, distress categories), so the
channel should be PRIVATE: set WEBHOOK_TOKEN for a protected/self-hosted ntfy,
or use a bearer-protected endpoint. The token is sent as `Authorization: Bearer`.
"""
from __future__ import annotations

import httpx

from .config import settings


def push(message: str, title: str, priority: str = "default",
         timeout: float = 8.0) -> bool:
    """POST a notification; return True on success, False (logged) on failure.
    No-op when no webhook is configured."""
    if not settings.webhook_url:
        return False
    headers = {"Title": title, "Priority": priority}
    if settings.webhook_token:
        headers["Authorization"] = f"Bearer {settings.webhook_token}"
    try:
        httpx.post(settings.webhook_url, content=message.encode("utf-8"),
                   headers=headers, timeout=timeout)
        return True
    except Exception as exc:  # delivery is best-effort
        print(f"[notify] webhook error: {exc}")
        return False
