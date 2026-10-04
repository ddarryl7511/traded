"""Optional notification hook: POSTs plain text to NOTIFY_WEBHOOK_URL (e.g. an ntfy.sh topic).
Failures are logged and never interrupt trading logic."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def make_notifier():
    url = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
    if not url:
        return lambda msg: log.info("notify: %s", msg)

    def send(msg: str) -> None:
        try:
            import requests
            if "discord.com/api/webhooks/" in url:  # Discord rejects plain text bodies
                requests.post(url, json={"content": f"📈 traded: {msg}"[:2000]}, timeout=10)
            else:
                requests.post(url, data=msg.encode("utf-8"), timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("notification failed: %s", type(exc).__name__)

    return send
