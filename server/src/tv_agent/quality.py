"""How well the agent does: from the history (each command's trace, tracing.py), and the nightly check of the voice
tests, the chained sentences and the episode and scene searches against their baselines. Stdlib only: the panel's
Quality tab uses it too.

summary()   per step (speech, deciding, the title, the episode, the scene, the streams, the start, the outcome): how
            often it went right, and its p50/p95 time; which rule or model decided and how often that was right; the
            models' calls and cost; the cost per day; the commonest failures, each with an example.
nightly()   runs scripts/bench_voice.py (the agent's own speech to text), bench_chains.py and bench_finding.py, nothing
            sent to the TV, compares each case with the baseline (the first run's, until one is accepted), and keeps
            the run in quality/runs/. A case right before and wrong now is run again, twice at most: a regression
            only when it's wrong every time, else "unsteady" (the models' answers vary from run to run). One that
            couldn't be checked (the hosted speech model or a subtitle addon failing) is neither. A regression
            leaves an alert (quality/alert.json) for the panel and the phones.
            `tv-agent quality nightly` runs it; the tv-agent-nightly timer every night.
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from .config import DATA_DIR

DIR = DATA_DIR / "quality"
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
STEPS = [("speech", "Speech to text"), ("decide", "Deciding"), ("title", "Finding the title"),
         ("episode", "Finding the episode"), ("scene", "Finding the scene"), ("streams", "Streams"),
         ("start", "Starting"), ("outcome", "The whole command")]
STARTS = {"play_title", "continue_watching", "next_episode", "pick", "sequence"}
SOURCES = {"tv": "TV", "phone": "Phone", "laptop": "Laptop mic", "text": "Typed", "mcp": "MCP"}


def _span(steps: list[dict]) -> float | None:
    """How long a stage took: from its first step's start to its last's end."""
    if not steps:
        return None
    return max(s["at"] for s in steps) - min(s["at"] - (s.get("ms") or 0) for s in steps)


def steps_of(e: dict) -> dict[str, tuple[bool | None, float | None]]:
    """Each step a command went through: (right, or None when it asked instead of guessing; how long in ms)."""
    trace = [t for t in e.get("trace") or [] if isinstance(t, dict)]
    timed = [t for t in trace if isinstance(t.get("at"), (int, float))]
    by = defaultdict(list)
    for t in timed:
        by[t.get("stage")].append(t)
    wrong = bool(e.get("wrong"))
    out: dict[str, tuple[bool | None, float | None]] = {}
    if e.get("stt_model") or e.get("stt") or e.get("stt_ms") is not None:
        out["speech"] = (bool(str(e.get("heard", "")).strip()) and not e.get("stt_error"), e.get("stt_ms"))
    out["decide"] = (e.get("type") != "none" and not wrong, e.get("decide_ms"))
    if title := by.get("title"):
        listed = any("listed" in str(t.get("verdict", "")) for t in title)
        picked = any(t.get("step") == "picked" for t in title)
        out["title"] = (None if listed else picked, _span(title))
    if episode := [t for t in by.get("episode", []) if t.get("step") == "verdict"]:
        v = episode[-1].get("verdict")
        out["episode"] = ({"play": True, "list": None}.get(v, False), _span(by["episode"]))
    if scene := by.get("scene"):
        jumped = any(t.get("step") == "jump" and t.get("verdict") == "done" for t in scene)
        listed = any(t.get("step") == "agreement" and "listed" in str(t.get("verdict", "")) for t in scene)
        out["scene"] = (True if jumped and not wrong else None if listed else False, _span(scene))
    start = by.get("start", [])
    if streams := [t for t in start if t.get("step") in ("streams", "candidates")]:
        out["streams"] = (any((t.get("kept") or 0) > 0 for t in streams), streams[0].get("ms"))
    if e.get("type") in STARTS and start:
        frame = next((t for t in start if t.get("step") == "first frame"), None)
        out["start"] = (bool(frame) and e.get("ok") is not False, e.get("ms"))
    total = sum(x for x in (e.get("stt_ms"), e.get("decide_ms"), e.get("ms")) if isinstance(x, (int, float)))
    out["outcome"] = (e.get("ok") is not False and not wrong, total or None)
    return out


def decided_by(e: dict) -> str:
    via, rule = e.get("via") or "?", e.get("rule") or ""
    if via in ("phrase", "pattern", "chain"):
        return f"{via}: {rule}" if rule else via
    if via == "d1":
        return f"d1 ({rule})" if rule else "d1"
    return via


def cost_of(e: dict) -> float:
    """What a command cost: its speech to text and every model call in its trace (else the decision's cost, for
    records from before the trace had them)."""
    calls = [t.get("cost") or 0 for t in e.get("trace") or []
             if isinstance(t, dict) and t.get("stage") in ("llm", "d1") and isinstance(t.get("at"), (int, float))]
    return (e.get("stt_cost") or 0) + (sum(calls) if calls else (e.get("cost") or 0))


def reason(e: dict) -> str:
    """A failure's kind: what it said, with names, numbers and quotes taken out."""
    if e.get("wrong") and e.get("ok") is not False:
        return "Marked “that's wrong”"
    say = str(e.get("say") or "nothing said")
    say = re.sub(r"“[^”]*”", "“…”", say)
    say = re.sub(r"\d+(?:[:.]\d+)*", "#", say)
    return say.split(" · ")[0][:90]


def _pct(xs: list[float], p: float) -> float | None:
    xs = sorted(xs)
    return xs[max(0, math.ceil(p * len(xs)) - 1)] if xs else None


def pick(entries: list[dict], since: float | None = None, sources: set[str] | None = None) -> list[dict]:
    return [e for e in entries if (since is None or e.get("t", 0) >= since)
            and (not sources or e.get("source") in sources)]


def summary(entries: list[dict]) -> dict:
    """The numbers of the Quality tab for these commands (pick() them first)."""
    per: dict[str, dict] = {k: {"label": label, "n": 0, "right": 0, "asked": 0, "ms": []} for k, label in STEPS}
    deciders: dict[str, list] = defaultdict(lambda: [0, 0])
    models: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost": 0.0, "ms": []})
    days: Counter = Counter()
    fails: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        for key, (ok, ms) in steps_of(e).items():
            s = per[key]
            s["n"] += 1
            s["right"] += ok is True
            s["asked"] += ok is None
            if isinstance(ms, (int, float)):
                s["ms"].append(ms)
        k = decided_by(e)
        deciders[k][0] += 1
        deciders[k][1] += e.get("ok") is not False and not e.get("wrong")
        for t in e.get("trace") or []:
            if isinstance(t, dict) and t.get("stage") in ("llm", "d1") and isinstance(t.get("at"), (int, float)):
                m = models[str((t.get("method") or {}).get("id") or t.get("stage"))]
                m["calls"] += 1
                m["cost"] += t.get("cost") or 0
                if t.get("ms") is not None:
                    m["ms"].append(t["ms"])
        if e.get("stt_model") or e.get("stt"):
            m = models[f"speech: {e.get('stt_model') or e.get('stt')}"]
            m["calls"] += 1
            m["cost"] += e.get("stt_cost") or 0
            if e.get("stt_ms") is not None:
                m["ms"].append(e["stt_ms"])
        days[time.strftime("%Y-%m-%d", time.localtime(e.get("t", 0)))] += cost_of(e)
        if e.get("ok") is False or e.get("wrong"):
            fails[reason(e)].append(e)
    steps = [{"key": k, "label": s["label"], "n": s["n"], "right": s["right"], "asked": s["asked"],
              "rate": s["right"] / s["n"] if s["n"] else None, "p50": _pct(s["ms"], 0.5), "p95": _pct(s["ms"], 0.95)}
             for k, s in per.items()]
    return {"n": len(entries), "steps": steps,
            "deciders": sorted(({"key": k, "n": n, "right": r, "rate": r / n} for k, (n, r) in deciders.items()),
                               key=lambda x: -x["n"]),
            "models": sorted(({"model": k, "calls": m["calls"], "cost": round(m["cost"], 5),
                               "p50": _pct(m["ms"], 0.5)} for k, m in models.items()), key=lambda x: -x["calls"]),
            "cost_by_day": sorted(((d, round(c, 5)) for d, c in days.items()), reverse=True),
            "failures": sorted(({"reason": r, "n": len(es), "example": max(es, key=lambda x: x.get("t", 0))["t"]}
                                for r, es in fails.items()), key=lambda x: -x["n"])}


# ---- the nightly check ----

BENCHES = {"voice": "Voice tests", "chains": "Sentences and chains", "finding": "Episodes and scenes"}


RECHECKS = 2          # a case that went wrong is run this many times more: a regression only if wrong every time


def _cases(name: str, data) -> dict[str, dict]:
    """A bench's results as case -> {"ok", "got", "cost"} (the voice tests by recording and speech model). "ok" is
    None for a case that couldn't be checked: the hosted speech model failed, a subtitle addon's downloads failed."""
    out = {}
    if name == "voice":
        for rec, by in (data.get("results") or {}).items():
            for spec, r in by.items():
                if isinstance(r, dict) and "ok" in r:
                    out[f"{rec} ({spec})"] = {"ok": None if r.get("error") else bool(r["ok"]),
                                              "got": r.get("error") or r.get("transcript"),
                                              "cost": (r.get("cost") or 0) + (r.get("stt_cost") or 0)}
    elif name == "chains":
        for r in data.get("rows") or []:
            out[r["text"]] = {"ok": bool(r["ok"]), "got": r.get("got"), "cost": r.get("cost") or 0}
    else:
        for r in data if isinstance(data, list) else []:
            if r.get("ok") is not None or r.get("unavailable"):
                out[f"{r['row']}. {r.get('wanted', '')} ({r['run']})"] = {
                    "ok": None if r.get("unavailable") else bool(r["ok"]),
                    "got": r.get("unavailable") or r.get("got"), "cost": r.get("cost") or 0}
    return out


def compare(base: dict[str, dict], now: dict[str, dict]) -> dict:
    common = set(base) & set(now)
    return {"right": sum(c["ok"] is True for c in now.values()), "of": sum(c["ok"] is not None for c in now.values()),
            "regressions": sorted(k for k in common if base[k]["ok"] is True and now[k]["ok"] is False),
            "fixes": sorted(k for k in common if base[k]["ok"] is False and now[k]["ok"] is True),
            "new": sorted(set(now) - set(base)),
            "unavailable": sorted(k for k, c in now.items() if c["ok"] is None),
            "cost": round(sum(c.get("cost") or 0 for c in now.values()), 5)}


def _only(name: str, keys: list[str], out: Path) -> list[str]:
    """The bench's arguments that run just these cases."""
    if name == "voice":
        return ["--recordings", ",".join(sorted({k.rsplit(" (", 1)[0] for k in keys}))]
    if name == "chains":
        texts = out.with_suffix(".texts.json")
        texts.write_text(json.dumps(sorted(keys), ensure_ascii=False))
        return ["--texts", str(texts)]
    return ["--rows", ",".join(sorted({k.split(".", 1)[0] for k in keys}, key=int))]


def _bench(name: str, stt: str, out: Path, only: list[str] | None = None) -> tuple[object, float]:
    """One bench run by the agent's own Python (its scripts' folder on the path): its saved results. `only`: just
    these cases."""
    python, t = sys.executable, time.monotonic()
    args = {"voice": ["bench_voice.py", "--stt", stt, "--save", str(out)],
            "chains": ["bench_chains.py", "--out", str(out)],
            "finding": ["bench_finding.py", "--out", str(out)]}[name] + (_only(name, only, out) if only else [])
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS.parent / "src")}
    subprocess.run([python, str(SCRIPTS / args[0]), *args[1:]], cwd=SCRIPTS.parent, env=env, timeout=3600,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.loads(out.read_text()), time.monotonic() - t


def recheck(name: str, stt: str, flagged: list[str], raw: Path) -> tuple[list[str], list[str], float, float]:
    """The cases that went wrong, run again (up to RECHECKS times, the ones still wrong each time): (wrong every
    time, right in a run again: the model's answers or the speech model's transcripts vary), and the reruns' cost
    and seconds. A case missing from a rerun stays wrong."""
    still, cost, secs = list(flagged), 0.0, 0.0
    for n in range(1, RECHECKS + 1):
        if not still:
            break
        data, took = _bench(name, stt, raw.with_name(f"{raw.stem}-recheck{n}.json"), still)
        again = _cases(name, data)
        cost += sum(again.get(k, {}).get("cost") or 0 for k in still)
        secs += took
        still = [k for k in still if again.get(k, {"ok": False})["ok"] is False]
    return still, [k for k in flagged if k not in still], round(cost, 5), secs


def nightly(stt: str, only: list[str] | None = None, folder: Path | None = None) -> dict:
    """The three benches against their baselines, what went wrong run again before it's called a regression; the
    run kept in runs/<date>.json, an alert on a regression."""
    folder = folder or DIR
    stamp = time.strftime("%Y-%m-%d-%H%M")
    for sub in (folder, folder / "runs", folder / "baselines"):     # the runs hold your transcripts: yours alone
        sub.mkdir(parents=True, exist_ok=True)
        sub.chmod(0o700)
    run = {"t": time.time(), "stt": stt, "benches": {}}
    for name in only or list(BENCHES):
        raw = folder / "runs" / f"{stamp}-{name}.json"
        try:
            data, secs = _bench(name, stt, raw)
            now = _cases(name, data)
            base_file = folder / "baselines" / f"{name}.json"
            if not base_file.exists():
                shutil.copy(raw, base_file)             # the first run is the baseline until one is accepted
            base = _cases(name, json.loads(base_file.read_text()))
            b = compare(base, now)
            if b["regressions"]:
                b["regressions"], b["unsteady"], more, took = recheck(name, stt, b["regressions"], raw)
                b["cost"] = round(b["cost"] + more, 5)
                secs += took
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            run["benches"][name] = {"error": f"{type(e).__name__}: {e}"[:200]}
            continue
        run["benches"][name] = {**b, "seconds": round(secs), "raw": raw.name,
                                "baseline": time.strftime("%Y-%m-%d", time.localtime(base_file.stat().st_mtime))}
    run["cost"] = round(sum(b.get("cost") or 0 for b in run["benches"].values()), 5)
    run["regressions"] = sum(len(b.get("regressions") or []) for b in run["benches"].values())
    (folder / "runs" / f"{stamp}.json").write_text(json.dumps(run, ensure_ascii=False, indent=1))
    if run["regressions"]:
        (folder / "alert.json").write_text(json.dumps({"run": f"{stamp}.json", "t": run["t"],
                                                       "regressions": run["regressions"], "seen": False}))
    return run


def runs(folder: Path | None = None, limit: int = 30) -> list[dict]:
    """The nightly runs kept, newest first (each with its file's name)."""
    folder = folder or DIR
    out = []
    for f in sorted((folder / "runs").glob("????-??-??-????.json"), reverse=True)[:limit]:
        try:
            out.append({**json.loads(f.read_text()), "file": f.name})
        except (OSError, ValueError):
            continue
    return out


def accept(run_file: str, folder: Path | None = None) -> list[str]:
    """A run's results as the new baselines (after a change made on purpose)."""
    folder = folder or DIR
    run = json.loads((folder / "runs" / run_file).read_text())
    done = []
    for name, b in run["benches"].items():
        if b.get("raw") and (folder / "runs" / b["raw"]).exists():
            shutil.copy(folder / "runs" / b["raw"], folder / "baselines" / f"{name}.json")
            done.append(name)
    seen(folder)                                        # its regressions were meant
    return done


def alert(folder: Path | None = None) -> dict | None:
    """The regression alert not seen yet, if any."""
    try:
        a = json.loads(((folder or DIR) / "alert.json").read_text())
    except (OSError, ValueError):
        return None
    return a if not a.get("seen") else None


def seen(folder: Path | None = None):
    path = (folder or DIR) / "alert.json"
    try:
        a = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    path.write_text(json.dumps({**a, "seen": True}))
