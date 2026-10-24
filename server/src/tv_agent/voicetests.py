"""Voice tests: commands said to the TV, kept with what they should have done, so scripts/bench_voice.py can replay
them through speech to text and the decision steps after a change. They're yours alone: in
~/.local/share/tv-agent/voice-tests (mode 700), never in the repo. Stdlib only (the panel adds tests too).

labels.jsonl, one line per recording, editable by hand:
  {"recording": "20261022-213000-123.wav", "expect": {"type": "play_title", "title": "The Bear", "season": 3},
   "exact": true, "text": "play the bear season three", "heard": "play the bear season three", "from": "history",
   "t": 1761161400.123}

- expect: the action it should give, as in eval/*.jsonl: the keys listed must match (strings ignoring case;
  "title_any": [...] any of these words, and so for any field: "episode_name_any", "seconds_any": [0, null]). With "exact", every other field must be at its default too, so "play
  The Bear" that comes out as season 2 is wrong; a sequence's steps are judged one by one. Film or series ("kind")
  and a guessed title's mark count only when a label names them.
- text: what was said, word for word, once someone checked it: the transcripts are judged against it.
- heard: what the agent heard at the time. from: history (seeded from the activity log), taught (what the panel
  taught it), panel ("Add to voice tests"), or anything you write.
A recording's audio is hard-linked here when it's labelled, so neither the agent keeping only its last 300
recordings nor erasing the history takes a test's audio with it. Removing a test: its line and its .wav.
"""
import fcntl
import json
import os
import re
import shutil
import statistics
import unicodedata
from collections import Counter
from pathlib import Path

from . import activity, stt
from .config import CONFIG_DIR, DATA_DIR
from .decide.numbers import TENS, UNITS

DIR = DATA_DIR / "voice-tests"
LABELS = DIR / "labels.jsonl"
# Your own cases for the benches that need no recording (the panel's "This was right / wrong"), kept here, never in
# the repo: sentences (typed commands, chains: scripts/bench_chains.py) and episodes or scenes (bench_finding.py)
SENTENCES = DIR / "sentences.jsonl"
FINDING = DIR / "finding.jsonl"
LEARNED = CONFIG_DIR / "learned.json"            # decide/learned.py's file (that module needs pydantic)
# Left out of what's expected unless a label names them: the words heard, a title played as a guess, and film or
# series (a search hint: "The Bear S3" is right with or without it)
UNCHECKED = ("heard", "guess", "kind")
# number words a transcript may give as digits ("a", "un"… are words first)
NUMBERS = {k: v for k, v in UNITS.items() if k not in ("a", "an", "oh", "un", "una")}


def load(path: Path = LABELS) -> list[dict]:
    """The labels, oldest recording first. A line that isn't one raises ValueError naming it."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            label = json.loads(line)
        except ValueError as e:
            raise ValueError(f"{path}:{n}: {e}") from None
        if not (isinstance(label, dict) and isinstance(label.get("recording"), str)
                and isinstance(label.get("expect"), dict) and "type" in label["expect"]):
            raise ValueError(f'{path}:{n}: a label needs "recording" and an "expect" with a "type"')
        out.append(label)
    return sorted(out, key=lambda x: x["recording"])


def _update(change, path: Path = LABELS):
    """Read, change and write the labels under a lock (the panel and the script both write them). A file that
    doesn't read is left as it is."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        labels = load(path)
        result = change(labels)
        f.seek(0)
        f.truncate()
        for label in sorted(labels, key=lambda x: x["recording"]):
            f.write(json.dumps(label, ensure_ascii=False) + "\n")
    return result


def audio(name: str, folder: Path = DIR, recordings: Path = stt.RECORDINGS) -> Path | None:
    """A test's audio: its own copy, else the agent's kept recording."""
    for path in (folder / Path(name).name, recordings / Path(name).name):
        if path.suffix == ".wav" and path.exists():
            return path
    return None


def keep_audio(name: str, folder: Path = DIR, recordings: Path = stt.RECORDINGS) -> bool:
    """Hard-link a kept recording in with the tests (copied across file systems); False when it's gone."""
    src, dst = recordings / Path(name).name, folder / Path(name).name
    if dst.exists():
        return True
    if src.suffix != ".wav" or not src.exists():
        return False
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)                   # mode 600 like the recording
    return True


def learned(path: Path = LEARNED) -> list[dict]:
    """The sentences taught in the panel (say -> do)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return [x for x in data if isinstance(x, dict) and isinstance(x.get("say"), str)
            and isinstance(x.get("do"), dict)] if isinstance(data, list) else []


def _plain(action: dict) -> dict:
    """An action as a label expects it: without the fields in UNCHECKED."""
    out = {k: v for k, v in action.items() if k not in UNCHECKED}
    if isinstance(out.get("steps"), list):
        out["steps"] = [_plain(s) if isinstance(s, dict) else s for s in out["steps"]]
    return out


def expected(entry: dict, taught: list[dict], asked: bool = False) -> tuple[dict | None, str]:
    """What a history entry should have done and where that comes from ("history" or "taught"), or None and why
    not. Seeding trusts only what worked and wasn't marked wrong; someone adding the test in the panel (asked)
    vouches for "didn't get that" and failed commands too."""
    if entry.get("taught"):
        said = entry.get("heard", "").strip().lower()
        do = next((x["do"] for x in reversed(taught) if x["say"].strip().lower() == said), None)
        return (_plain(do), "taught") if do else (None, "taught, but no longer in learned.json")
    if entry.get("wrong"):
        return None, "marked wrong: teach it the right action first"
    action = entry.get("action")
    if not isinstance(action, dict) or "type" not in action:
        return None, "no action in the history"
    if not asked and action["type"] == "none":
        return None, "not understood"
    if not asked and entry.get("ok") is False:
        return None, "didn't work"
    return _plain(action), "history"


def label(entry: dict, taught: list[dict], asked: bool = False, text: str | None = None) -> tuple[dict | None, str]:
    """A history entry's voice test (exact: every field as it was decided), or None and why not."""
    if not entry.get("recording"):
        return None, "no recording"
    expect, why = expected(entry, taught, asked)
    if expect is None:
        return None, why
    out = {"recording": entry["recording"], "expect": expect, "exact": True}
    if text and text.strip():
        out["text"] = text.strip()
    return out | {"heard": entry.get("heard", ""), "from": "panel" if asked else why, "t": entry.get("t")}, why


def seed(entries: list[dict], taught: list[dict], labels: list[dict], has_audio=lambda name: True) -> Counter:
    """Label the history's recordings that have none yet (labels is changed in place). Counts "added", "labelled"
    (already were) and why the others weren't. A label seeded from the history and not edited since follows a
    later "that's wrong" or teaching: "updated" or "removed"."""
    by_name = {x["recording"]: x for x in labels}
    done = Counter()
    for e in sorted(entries, key=lambda e: e.get("t", 0)):
        name = e.get("recording")
        if not name:
            continue
        have = by_name.get(name)
        stale = (have is not None and have.get("from") == "history" and (e.get("wrong") or e.get("taught"))
                 and have["expect"] == _plain(e.get("action") or {}))
        if have is not None and not stale:
            done["labelled"] += 1
            continue
        new, why = label(e, taught) if has_audio(name) else (None, "recording gone")
        if have is not None:
            labels.remove(have)
            done["updated" if new else "removed"] += 1
        elif new:
            done["added"] += 1
        else:
            done[why] += 1
        if new:
            labels.append(new)
            by_name[name] = new
    return done


def seed_history(path: Path = LABELS, history: Path = activity.FILE, taught: Path = LEARNED,
                 recordings: Path = stt.RECORDINGS) -> Counter:
    """seed() from the activity log, written to the labels file, each test's audio kept with it."""
    entries = [e for e in activity.read(history) if e.get("recording")]
    if not entries:
        return Counter()
    done = _update(lambda labels: seed(entries, learned(taught), labels,
                                       lambda name: audio(name, path.parent, recordings) is not None), path)
    for x in load(path):
        keep_audio(x["recording"], path.parent, recordings)
    return done


def add(new: dict, path: Path = LABELS, recordings: Path = stt.RECORDINGS) -> bool:
    """Add one recording's test, or replace the one it had (True); its audio is kept with the tests."""
    keep_audio(new["recording"], path.parent, recordings)

    def change(labels: list[dict]) -> bool:
        old = [x for x in labels if x["recording"] == new["recording"]]
        labels[:] = [x for x in labels if x["recording"] != new["recording"]] + [new]
        return bool(old)
    return _update(change, path)


def describe(expect: dict) -> str:
    """'play title · title The Bear · season 3'; a sequence's steps joined by 'then'."""
    if isinstance(expect.get("steps"), list):
        return " then ".join(describe(s) for s in expect["steps"] if isinstance(s, dict))
    kind = expect.get("type", "?")
    parts = ["nothing (not a command)" if kind == "none" else str(kind).replace("_", " ")]
    for k, v in expect.items():
        if k != "type" and v not in (None, False):
            parts.append(f"{k.replace('_', ' ')} {'/'.join(map(str, v)) if isinstance(v, list) else v}")
    return " · ".join(parts)


# ---- transcripts ----

def words(text: str) -> list[str]:
    """The words of a transcript as compared: case, accents and punctuation don't count, and numbers are digits
    ("three" = "3", "twenty-one" = "21", "treinta y cinco" = "35")."""
    s = unicodedata.normalize("NFKD", text.lower().replace("'", "").replace("’", ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    ws, out, i = re.sub(r"[^\w\s]", " ", s).split(), [], 0
    while i < len(ws):
        w, i = ws[i], i + 1
        if w in TENS:
            n, j = TENS[w], i + (i < len(ws) and ws[i] == "y")
            if j < len(ws) and NUMBERS.get(ws[j], 10) < 10:
                n, i = n + NUMBERS[ws[j]], j + 1
            w = str(n)
        elif w in NUMBERS:
            w = str(NUMBERS[w])
        out.append(w)
    return out


def wer(said: str, heard: str) -> float:
    """Word error rate: words substituted, missed and added, per word said."""
    ref, hyp = words(said), words(heard)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        diagonal, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            diagonal, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, diagonal + (r != h))
    return row[-1] / max(len(ref), 1)


# ---- runs: results[recording][speech model] = {"transcript", "text_ok", "wer", "ok", "kind", "got", "via",
#      "stt_ms", "decide_ms", "cost", "expect", "error"} ----

def totals(results: dict) -> dict:
    """Per speech model: how many right (action, its kind, transcript), the word error rate, the timings and cost."""
    out: dict[str, dict] = {}
    for by_model in results.values():
        for model, r in by_model.items():
            t = out.setdefault(model, {"n": 0, "ok": 0, "kind": 0, "texts": 0, "text_ok": 0, "wer": [], "stt_ms": [],
                                       "decide_ms": [], "via": Counter(), "cost": 0.0, "errors": 0})
            t["n"] += 1
            if r.get("error"):
                t["errors"] += 1
                continue
            t["ok"] += bool(r.get("ok"))
            t["kind"] += bool(r.get("kind"))
            if r.get("text_ok") is not None:
                t["texts"] += 1
                t["text_ok"] += r["text_ok"]
                t["wer"].append(r["wer"])
            for k in ("stt_ms", "decide_ms"):
                if r.get(k) is not None:
                    t[k].append(r[k])
            t["via"][r.get("via", "?")] += 1
            t["cost"] += r.get("cost") or 0
    for t in out.values():
        t["wer"] = statistics.mean(t["wer"]) if t["wer"] else None
    return out


def compare(before: dict, after: dict) -> dict:
    """What changed between two runs (bench_voice.py --save): "regressions" and "fixes" (the action, or the
    transcript against the label's words), "relabelled" (the label changed, so it isn't the code), "changed" (other
    transcripts that differ), "errors" (a model failed in one of them) and the pairs "only_before" / "only_after"."""
    out = {k: [] for k in ("regressions", "fixes", "relabelled", "changed", "errors", "only_before", "only_after")}
    old, new = before.get("results", {}), after.get("results", {})
    for name in sorted(set(old) | set(new)):
        for model in sorted(set(old.get(name, {})) | set(new.get(name, {}))):
            a, b = old.get(name, {}).get(model), new.get(name, {}).get(model)
            if a is None or b is None:
                out["only_after" if a is None else "only_before"].append((name, model))
                continue
            item = {"recording": name, "stt": model, "before": a, "after": b}
            if a.get("error") or b.get("error"):
                out["errors"].append(item)
                continue
            if a.get("expect") != b.get("expect") and a.get("ok") != b.get("ok"):
                out["relabelled"].append(item)
                continue
            moved = False
            for key, what in (("ok", "action"), ("text_ok", "transcript")):
                if a.get(key) is not None and b.get(key) is not None and a[key] != b[key]:
                    out["fixes" if b[key] else "regressions"].append(item | {"what": what})
                    moved = True
            if not moved and words(a.get("transcript", "")) != words(b.get("transcript", "")):
                out["changed"].append(item)
    return out


def cases(path: Path) -> list[dict]:
    """Your own cases of a bench (SENTENCES, FINDING)."""
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def add_case(path: Path, case: dict, key: str) -> bool:
    """One of your cases, replacing the one with the same `key` field (True when it replaced one)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        have = [json.loads(line) for line in f.read().splitlines() if line.strip()]
        kept = [c for c in have if c.get(key) != case.get(key)]
        f.seek(0)
        f.truncate()
        for c in [*kept, case]:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return len(kept) < len(have)


# ---- a command of the history kept as a test (the panel's “This was right / wrong”) ----

def asked(e: dict, stage: str) -> dict | None:
    """What an episode or scene search of this command was asked (its trace's "asked" step), and what it found: the
    episode's id, or the time it jumped to."""
    trace = [t for t in e.get("trace") or [] if isinstance(t, dict) and t.get("stage") == stage]
    first = next((t for t in trace if t.get("step") == "asked"), None)
    if not first:
        return None
    if stage == "episode":
        done = next((t for t in trace if t.get("step") == "verdict"), {})
        return {**first, "found": done.get("episode")}
    jump = next((t for t in trace if t.get("step") == "jump" and t.get("verdict") == "done"), None)
    return {**first, "found": jump.get("to") if jump else None}


def _secs(clock: str) -> int | None:
    m = re.fullmatch(r"\s*(?:(\d+):)?(\d{1,2}):(\d\d)\s*", clock or "")
    return None if not m else int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _episode_id(imdb: str, said: str) -> str | None:
    m = re.fullmatch(r"\s*[sS](\d+)\s*[eE](\d+)\s*", said or "")
    return f"{imdb}:{int(m.group(1))}:{int(m.group(2))}" if m else None


def keep_command(e: dict, expect: dict | None, words: str, right: bool, episode: str | None = None,
                 moment: str | None = None) -> list[str]:
    """A command of the history as tests: a voice test when its recording is kept (else one of your sentences, by
    its words), and its episode or scene case: the episode (S2E5) or moment (m:ss) given, or, when it was right,
    what it found. What was added, in words."""
    added = []
    words = words.strip()
    if expect and e.get("recording") and stt.recording(e["recording"]):
        label = {"recording": e["recording"], "expect": expect, "exact": True, "heard": e.get("heard", ""),
                 "from": "panel: right" if right else "panel: wrong, should be this", "t": e.get("t")}
        if words:
            label["text"] = words
        add(label, LABELS, stt.RECORDINGS)
        added.append("a voice test")
    elif expect and words:
        lang = "es" if re.search(r"[ñáéíóú¿¡]|\b(?:pon|el|la|que|del|y|luego)\b", words.lower()) else "en"
        add_case(SENTENCES, {"lang": lang, "text": words, "expect": expect, "from": "panel", "t": e.get("t")}, "text")
        added.append("one of your sentences")
    if (ep := asked(e, "episode")) and (episode or right and ep.get("found")):
        if want := _episode_id(ep["imdb"], episode) if episode else ep["found"]:
            add_case(FINDING, {"find": "episode", "imdb": ep["imdb"], "name": ep.get("title", ""),
                               "wanted": ep["wanted"], "expect": [want]}, "wanted")
            added.append("an episode case")
    if (sc := asked(e, "scene")) and (moment or right and sc.get("found")):
        if (at := _secs(moment or sc["found"])) is not None:
            add_case(FINDING, {"find": "scene", "video": sc["video"], "name": sc.get("title", ""),
                               "wanted": sc["wanted"], "expect": [max(0, at - 20), at + 20]}, "wanted")
            added.append("a scene case")
    return added
