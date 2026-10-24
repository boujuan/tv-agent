"""The activity log: one JSON line per command (what was heard, how it was decided, what happened), in
~/.local/share/tv-agent/activity.jsonl (mode 600). The panel shows, exports, imports and erases it; both sides
lock the file while writing. Nothing is written when log_transcripts is off. "That's wrong" marks the last entry
(`wrong`); the panel lists those and teaches the right action (learned.json).

Each command's trace (tracing.py) is in its line; what's too big for one (the plots it read, subtitle passages,
model prompts and answers) is in its own file, traces/<t>.json.gz (mode 600), named by the line's "details". The
newest KEEP_DETAILS are kept; Erase removes them; an export can carry them ("details_data"), an import puts them back.
"""
import fcntl
import gzip
import json
import os
import re
import time
from pathlib import Path

from . import __version__
from .config import DATA_DIR

FILE = DATA_DIR / "activity.jsonl"
TRACES = DATA_DIR / "traces"
KEEP_DETAILS = 500


def save_details(t: float, data: dict, folder: Path | None = None) -> str:
    """A command's big parts, gzipped JSON beside the history; the name its line keeps. The oldest beyond
    KEEP_DETAILS go."""
    folder = folder or TRACES
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"{t:.3f}.json.gz"
    fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(gzip.compress(json.dumps(data, ensure_ascii=False).encode(), 6))
    files = sorted(folder.glob("*.json.gz"))
    for old in files[:-KEEP_DETAILS]:
        old.unlink(missing_ok=True)
    return name


def details(entry: dict, folder: Path | None = None) -> dict | None:
    """A line's big parts, if it has them and they're still kept (else what an import carried)."""
    folder = folder or TRACES
    if isinstance(entry.get("details_data"), dict):
        return entry["details_data"]
    name = str(entry.get("details") or "")
    if not re.fullmatch(r"[0-9.]+\.json\.gz", name):
        return None
    try:
        return json.loads(gzip.decompress((folder / name).read_bytes()))
    except (OSError, ValueError, EOFError):
        return None


def append(entry: dict, path: Path | None = None):
    path = path or FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({"t": round(time.time(), 3), **entry}, ensure_ascii=False) + "\n")


def entry(source: str, heard: str, decision, ok: bool | None, say: str, ms: float | None,
          extra: dict | None = None) -> dict:
    """One record. source: 'tv' (remote mic), 'laptop' (panel mic), 'text' (typed), 'mcp'. ms: doing it. extra:
    how it was heard and timed (stt_model, stt_where, stt_language, stt_vocabulary, stt_ms, stt_cost,
    after_speech_ms, audio_s, recording) and how the action went (the agent's details); decide_ms, the models'
    cost and the trace (each step tried, with the model that answered) come from the decision. Rich enough to
    debug a command later, or to train on."""
    e = {"agent": __version__, "source": source, "heard": heard, "type": decision.action.type, "via": decision.via,
         "rule": decision.rule, "confidence": round(decision.confidence, 3),
         "action": decision.action.model_dump(exclude_none=True), "ok": ok, "say": say,
         "ms": round(ms) if ms is not None else None,
         "decide_ms": round(decision.ms, 2) if getattr(decision, "ms", 0) else None,      # rules: well under 1 ms
         "cost": getattr(decision, "cost", 0) or None, "trace": getattr(decision, "trace", None) or None}
    e.update({k: v for k, v in (extra or {}).items() if v is not None})
    return {k: v for k, v in e.items() if v is not None or k in ("ok", "ms")}


def read(path: Path | None = None, limit: int | None = None) -> list[dict]:
    """Newest first."""
    path = path or FILE
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = _parse(lines)
    out.sort(key=lambda e: e["t"], reverse=True)
    return out[:limit] if limit else out


def _parse(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and "t" in e:
            out.append(e)
    return out


def _update(change, path: Path):
    """Read, change and write the whole file under its lock, so an entry appended meanwhile isn't lost."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        entries = _parse(f.read().splitlines())
        result = change(entries)
        f.seek(0)
        f.truncate()
        for e in sorted(entries, key=lambda e: e["t"]):
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return result


def merge(new: list[dict], path: Path | None = None, folder: Path | None = None) -> int:
    """Add entries (e.g. imported), skipping ones already there (same time and words). Returns how many. Big parts an
    export carried ("details_data") go back into their own files."""
    path, folder = path or FILE, folder or TRACES
    def change(have: list[dict]) -> int:
        seen = {(round(float(e["t"]), 1), e.get("heard", "")) for e in have}
        added = [e for e in new if isinstance(e, dict) and "t" in e
                 and (round(float(e["t"]), 1), e.get("heard", "")) not in seen]
        for e in added:
            if isinstance(data := e.pop("details_data", None), dict):
                e["details"] = save_details(float(e["t"]), data, folder)
        have.extend(added)
        return len(added)
    return _update(change, path)


def erase(path: Path | None = None, folder: Path | None = None):
    path, folder = path or FILE, folder or TRACES
    _update(lambda entries: entries.clear(), path)
    for f in folder.glob("*.json.gz") if folder.is_dir() else []:
        f.unlink(missing_ok=True)


def mark(t: float, wrong: bool, path: Path | None = None, heard: str | None = None, **extra) -> dict | None:
    """Mark the entry at time t (with these words, when given) as wrong or not, with any extra fields (e.g. what it
    was taught as)."""
    path = path or FILE
    def change(entries: list[dict]) -> dict | None:
        for e in entries:
            if abs(float(e["t"]) - t) < 0.001 and (heard is None or e.get("heard") == heard):
                e["wrong"] = wrong
                e.update(extra)
                return e
        return None
    return _update(change, path)


def mark_last_wrong(path: Path | None = None) -> dict | None:
    """"That's wrong": the newest command before it is marked."""
    path = path or FILE
    for e in read(path, limit=5):
        if e.get("type") != "wrong":
            return mark(float(e["t"]), True, path, heard=e.get("heard"))
    return None
