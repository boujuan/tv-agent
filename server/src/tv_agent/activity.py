"""The activity log: one JSON line per command (what was heard, how it was decided, what happened), in
~/.local/share/tv-agent/activity.jsonl (mode 600). The panel shows, exports, imports and erases it; both sides
lock the file while writing. Nothing is written when log_transcripts is off. "That's wrong" marks the last entry
(`wrong`); the panel lists those and teaches the right action (learned.json)."""
import fcntl
import json
import os
import time
from pathlib import Path

from .config import DATA_DIR

FILE = DATA_DIR / "activity.jsonl"


def append(entry: dict, path: Path = FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({"t": round(time.time(), 3), **entry}, ensure_ascii=False) + "\n")


def entry(source: str, heard: str, decision, ok: bool | None, say: str, ms: float | None) -> dict:
    """One record. source: 'tv' (remote mic), 'laptop' (panel mic), 'text' (typed), 'mcp'."""
    return {"source": source, "heard": heard, "type": decision.action.type, "via": decision.via,
            "rule": decision.rule, "confidence": round(decision.confidence, 3),
            "action": decision.action.model_dump(exclude_none=True), "ok": ok, "say": say,
            "ms": round(ms) if ms is not None else None}


def read(path: Path = FILE, limit: int | None = None) -> list[dict]:
    """Newest first."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and "t" in e:
            out.append(e)
    out.sort(key=lambda e: e["t"], reverse=True)
    return out[:limit] if limit else out


def _rewrite(entries: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        f.truncate()
        for e in sorted(entries, key=lambda e: e["t"]):
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def merge(new: list[dict], path: Path = FILE) -> int:
    """Add entries (e.g. imported), skipping ones already there (same time and words). Returns how many."""
    have = read(path)
    seen = {(round(float(e["t"]), 1), e.get("heard", "")) for e in have}
    added = [e for e in new if isinstance(e, dict) and "t" in e and (round(float(e["t"]), 1), e.get("heard", "")) not in seen]
    if added:
        _rewrite(have + added, path)
    return len(added)


def erase(path: Path = FILE):
    _rewrite([], path)


def mark(t: float, wrong: bool, path: Path = FILE, heard: str | None = None, **extra) -> dict | None:
    """Mark the entry at time t (with these words, when given) as wrong or not, with any extra fields (e.g. what it
    was taught as)."""
    entries = read(path)
    for e in entries:
        if abs(float(e["t"]) - t) < 0.001 and (heard is None or e.get("heard") == heard):
            e["wrong"] = wrong
            e.update(extra)
            _rewrite(entries, path)
            return e
    return None


def mark_last_wrong(path: Path = FILE) -> dict | None:
    """"That's wrong": the newest command before it is marked."""
    for e in read(path, limit=5):
        if e.get("type") != "wrong":
            return mark(float(e["t"]), True, path, heard=e.get("heard"))
    return None
