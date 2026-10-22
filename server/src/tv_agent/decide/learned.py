"""Sentences you taught: "that's wrong" marks a command, and the panel's Activity tab teaches what it should have
done. Each is an exact sentence (as cleaned up) and its action, in ~/.config/tv-agent/learned.json, checked before
the rules' own fixed phrases. Kept apart from rules.json, so the default rules still get their updates."""
import json
import os
import time
from pathlib import Path

from .. import actions as A
from ..config import CONFIG_DIR

FILE = CONFIG_DIR / "learned.json"


def load(path: Path = FILE) -> list[dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [x for x in data if isinstance(x, dict) and isinstance(x.get("say"), str) and isinstance(x.get("do"), dict)]


def _save(items: list[dict], path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def add(say: str, do: dict, path: Path = FILE) -> dict:
    """Teach one sentence (replacing what it was taught before); the action is checked first."""
    action = A.parse(do)
    if action.type in ("none", "wrong"):
        raise ValueError("teach an action, not “didn't get that”")
    item = {"say": say.strip(), "do": action.model_dump(exclude_none=True, exclude_defaults=True) | {"type": action.type},
            "t": round(time.time())}
    items = [x for x in load(path) if x["say"].lower() != item["say"].lower()] + [item]
    _save(items, path)
    return item


def remove(say: str, path: Path = FILE) -> bool:
    items = load(path)
    keep = [x for x in items if x["say"].lower() != say.strip().lower()]
    _save(keep, path)
    return len(keep) != len(items)


def mtime(path: Path = FILE) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
