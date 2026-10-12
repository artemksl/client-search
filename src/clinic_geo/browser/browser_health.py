"""Read-only diagnostics for the persistent Chrome CDP endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from typing import Optional
import urllib.request
from urllib.parse import urlsplit


DEFAULT_CDP_URL = "http://127.0.0.1:9222"


def cdp_endpoint_health(
    cdp_url: Optional[str] = None,
    *,
    timeout_seconds: float = 0.75,
) -> dict:
    """Return a small process/session fingerprint without mutating Chrome.

    The websocket browser id changes when the CDP browser process/session is
    replaced. A reachable endpoint with a disconnected Playwright client points
    to client/target loss; an unreachable endpoint points to shared-CDP loss.
    """

    endpoint = str(cdp_url or os.getenv("CDP_URL") or DEFAULT_CDP_URL).rstrip("/")
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        with urllib.request.urlopen(
            endpoint + "/json/version",
            timeout=max(0.05, float(timeout_seconds)),
        ) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise ValueError("CDP version endpoint returned a non-object payload")
        websocket_url = str(payload.get("webSocketDebuggerUrl") or "")
        websocket_path = urlsplit(websocket_url).path.rstrip("/")
        browser_session_id = websocket_path.rsplit("/", 1)[-1] if websocket_path else None
        return {
            "checked_at": checked_at,
            "endpoint": endpoint,
            "reachable": True,
            "browser": str(payload.get("Browser") or "") or None,
            "protocol_version": str(payload.get("Protocol-Version") or "") or None,
            "browser_session_id": browser_session_id,
            "error": None,
        }
    except Exception as exc:
        return {
            "checked_at": checked_at,
            "endpoint": endpoint,
            "reachable": False,
            "browser": None,
            "protocol_version": None,
            "browser_session_id": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def cdp_session_changed(before: object, after: object) -> Optional[bool]:
    """Compare two health rows, returning None when comparison is unavailable."""

    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    first = str(before.get("browser_session_id") or "")
    second = str(after.get("browser_session_id") or "")
    if not first or not second:
        return None
    return first != second


def classify_browser_transport(
    *,
    client_connected: bool,
    page_closed: bool,
    before: object = None,
    after: object = None,
) -> str:
    """Give a conservative scope to a Playwright transport/target failure."""

    if client_connected:
        return "page_target_closed" if page_closed else "page_or_context_error"
    if isinstance(after, dict) and after.get("reachable"):
        if cdp_session_changed(before, after) is True:
            return "shared_browser_session_replaced"
        return "worker_cdp_client_disconnected_shared_browser_reachable"
    return "shared_cdp_endpoint_unreachable"
