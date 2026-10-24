"""Every command's whole story, for the history (activity.py) and the panel's Decisions tab: each step it took after
the words were heard, deciding and doing, timed and labelled, with how it was done and how sure it was.

A step (one dict, small: it goes in activity.jsonl):
  stage, step   what part ("decide", "title", "episode", "scene", "start", "chain", "llm", "d1"…) and which step
  label         what the panel shows ("Which episode", "Spanish subtitles")
  at, ms        when it ended, in ms since the command began (negative: before, like the speech to text), and how
                long it took
  method        how: {"kind": "rule" | "llm" | "d1" | "stt" | "local" | "addon" | "web" | "cache" | "player", "id": the
                rule, model or source}
  confidence, threshold   how sure, against the bar it had to pass
  verdict, why  what came of it ("accepted", "refused", "listed", "played", "failed"…) and why
  detail        the key of what's too big for the history (the plots it read, the subtitle passages, a model's
                prompt and answer): kept in the command's own file beside it (activity.save_details)
  …             what it found, summarised
Never a link, a key or a debrid URL: callers pass summaries.

One Trace per command (Service.decide makes it, Service.execute finishes it), carried by a ContextVar through the
decision, the action and the tasks it starts; what a task does after the command is recorded (a prefetch, the
episode check) is left out.
"""
import time
from contextvars import ContextVar

DETAIL_TEXT = 60_000                 # characters of one detail kept (a prompt with a film's subtitles)


class Trace:
    def __init__(self):
        self.t0 = time.perf_counter()
        self.steps: list[dict] = []
        self.details: dict[str, object] = {}
        self.closed = False

    def at(self, when: float | None = None) -> int:
        return round(((when or time.perf_counter()) - self.t0) * 1000)

    def close(self) -> tuple[list[dict], dict]:
        """The steps (in the order they ended) and the details; nothing is added after."""
        self.closed = True
        return sorted(self.steps, key=lambda s: s.get("at", 0)), self.details


TRACE: ContextVar[Trace | None] = ContextVar("trace", default=None)


def method(kind: str, id: str | None = None) -> dict:
    return {"kind": kind, **({"id": id} if id else {})}


def _trimmed(value, room: int = DETAIL_TEXT):
    if isinstance(value, str):
        return value if len(value) <= room else value[:room] + f"… ({len(value) - room} more characters)"
    if isinstance(value, dict):
        return {k: _trimmed(v, room) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_trimmed(v, room) for v in value]
    return value


def step(stage: str, step: str, *, label: str = "", ms: float | None = None, done: float | None = None,
         method: dict | None = None, detail=None, **fields) -> dict | None:
    """One step into the command's trace (None outside a command, or once it's recorded). `done`: when it ended
    (time.perf_counter()), if not now; `detail`: what's too big for the history."""
    tr = TRACE.get()
    if tr is None or tr.closed:
        return None
    entry = {"stage": stage, "step": step, **({"label": label} if label else {}),
             **({"ms": round(ms)} if ms is not None else {}), "at": tr.at(done)}
    if method:
        entry["method"] = method
    if detail is not None:
        key = f"{stage}.{step}.{len(tr.details) + 1}"
        tr.details[key] = _trimmed(detail)
        entry["detail"] = key
    entry.update({(f"{k}_" if k in entry else k): v for k, v in fields.items() if v is not None})   # never its own
    tr.steps.append(entry)
    return entry


def since(t: float) -> float:
    """ms since a time.perf_counter() reading."""
    return (time.perf_counter() - t) * 1000
