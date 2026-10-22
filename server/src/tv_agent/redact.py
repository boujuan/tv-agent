"""Keep secrets out of logs: the Stremio auth key, debrid keys inside addon and stream URLs, the agent token.

Every log record passes through RedactFilter: known secret values become ***, and any http(s) URL is cut down to
its scheme and host, because addon transport URLs and debrid stream URLs carry API keys in their paths.
"""
import logging
import re

URL = re.compile(r"\b(https?)://([^/\s\"'<>]+)[^\s\"'<>]*")
_known: set[str] = set()


def remember(*values: str | None):
    """Register secret values to mask wherever they appear."""
    for v in values:
        if v and len(v) >= 6:
            _known.add(v)


def scrub(text: str) -> str:
    for v in _known:
        text = text.replace(v, "***")
    return URL.sub(lambda m: f"{m.group(1)}://{m.group(2)}/…", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        return True


def setup_logging(level=logging.INFO):
    handler = logging.StreamHandler()
    handler.addFilter(RedactFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Libraries that log full request URLs: keep them quiet (the filter would scrub them anyway).
    for name in ("httpx", "httpcore", "uvicorn.access", "adb_shell"):
        logging.getLogger(name).setLevel(logging.WARNING)
