"""Logging to stderr (captured by journald under systemd) and a rotating file, with a filter that
redacts the API key/secret if they ever appear in a message."""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from .alpaca_api import KEY_ENV, SECRET_ENV


class RedactSecrets(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        for env in (KEY_ENV, SECRET_ENV, "NOTIFY_WEBHOOK_URL"):
            val = os.environ.get(env, "")
            if len(val) >= 6 and val in msg:
                msg = msg.replace(val, "***REDACTED***")
                record.msg, record.args = msg, ()
        return True


def setup_logging(log_file: Path, verbose: bool = False) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in (logging.StreamHandler(),
              logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5)):
        h.setFormatter(fmt)
        h.addFilter(RedactSecrets())
        root.addHandler(h)
    # urllib3 debug logs can include request URLs; keep them quiet.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
