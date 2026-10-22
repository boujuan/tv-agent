"""The decision rules as data: ~/.config/tv-agent/rules.json, or the defaults shipped next to this file.

    normalize  fillers removed at the start ("hey tv", "can you"), words removed anywhere ("please"), and
               fixes for common mishearings (regex -> replacement)
    phrases    exact sentences -> one fixed action                        ("pause" -> pause)
    patterns   ordered regexes with named parts -> an action template      ("skip forward (?P<amount>.+)")
               Template values are literals or "{part|converter}"; a converter ending in "!" makes the rule
               not match when that part is there but can't be converted. "*" merges a converter's fields.
    model      the d1-3B question, its options (built-in intents and your own, which use "{text|…}"),
               the confidence below which nothing happens, and the title-choice question
    apps       the apps the agent may open (package, activity, spoken names)
    hooks      which app handles YouTube, Twitch and Stremio pages, and the link it's sent

The daemon loads this once, checks it fully, and swaps it in only if it's valid (rules can't break the agent).
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from pydantic import ValidationError
from rapidfuzz import fuzz, process

from .. import actions as A
from ..config import CONFIG_DIR
from . import text as T
from .numbers import seconds_in, to_int

DEFAULTS_FILE = Path(__file__).with_name("default_rules.json")
USER_FILE = CONFIG_DIR / "rules.json"
BUILTIN_INTENTS = ("play_title", "continue_watching", "next_episode", "previous_episode", "open_in_stremio", "youtube",
                   "twitch", "open_app", "home", "pause", "resume", "stop", "skip_forward", "skip_back", "seek_to",
                   "restart", "volume_set", "volume_up", "volume_down", "mute", "unmute",
                   "transport", "volume", "none")    # transport/volume: the broader intents of rules files before v2
PLACEHOLDER = re.compile(r"^\{(\w+)(?:\|(\w+)(!)?)?\}$")
PACKAGE = re.compile(r"^[a-zA-Z][\w]*(?:\.[\w]+)+$")
ACTIVITY = re.compile(r"^[.\w$]+$")
LINK_SCHEMES = ("https://", "http://", "twitch://", "stremio://", "vnd.youtube:", "intent:")


class NoMatch(Exception):
    pass


class RulesError(ValueError):
    pass


@dataclass(frozen=True)
class App:
    key: str
    label: str
    package: str
    activity: str
    aliases: tuple[str, ...]

    @property
    def component(self) -> str:
        return f"{self.package}/{self.activity}"


@dataclass
class Pattern:
    id: str
    stage: str              # "grammar" (playback, volume…) or "cue" (YouTube, apps…): only for the diagram
    regex: re.Pattern
    do: dict
    note: str = ""


@dataclass
class Rules:
    raw: dict
    source: str
    fillers: re.Pattern
    remove_words: re.Pattern | None
    fixes: list[tuple[re.Pattern, str]]
    phrases: dict[str, tuple[dict, str]]         # sentence -> (action, phrase group id)
    patterns: list[Pattern]
    question: str
    min_confidence: float
    intents: dict[str, dict]                     # key -> {"describe", "do"?}
    fallback_intent: str
    title_question: str
    title_candidates: int
    apps: dict[str, App]
    hooks: dict[str, dict]
    counts: dict = field(default_factory=dict)
    min_ratio: float = 1.0                       # d1's top answer must be this many times as likely as its second

    # ---- apps and hooks ----

    def find_app(self, spoken: str) -> App | None:
        names = {alias: app for app in self.apps.values() for alias in app.aliases}
        hit = process.extractOne(spoken.lower(), list(names), scorer=fuzz.WRatio, score_cutoff=80)
        return names[hit[0]] if hit else None

    def link(self, hook: str, **values) -> tuple[App, str]:
        h = self.hooks[hook]
        template = h["link_episode"] if values.get("video") and h.get("link_episode") else h["link"]
        return self.apps[h["app"]], template.format(**{k: quote(str(v), safe=":") for k, v in values.items()})

    # ---- templates ----

    def convert(self, value: str | None, conv: str | None, strict: bool):
        if value is None:
            return None
        out = CONVERTERS[conv or "text"](value, self)
        if out is None and strict:
            raise NoMatch
        return out

    def fill(self, template: dict, groups: dict) -> dict:
        action = {}
        for key, val in template.items():
            m = PLACEHOLDER.match(val) if isinstance(val, str) else None
            if not m:
                action[key] = val
                continue
            got = self.convert(groups.get(m.group(1)), m.group(2), bool(m.group(3)))
            if key == "*":
                if got:
                    action.update(got)
            elif got is not None:
                action[key] = got
        model = A.ACTION_TYPES.get(action.get("type"))
        if model:
            action = {k: v for k, v in action.items() if k in model.model_fields and v is not None}
        return action


# ---- converters: text -> value (None means "nothing usable") ----

def _time(v, _):
    s = seconds_in(v)
    if s is None and (n := to_int(v)) is not None:
        s = n * 60            # "go to minute twelve", "jump to 12"
    return s


def _app(v, rules):
    app = rules.find_app(v)
    return app.key if app and len(v.split()) <= 3 else None


def _alias(v, rules):
    for app in rules.apps.values():
        if v.strip() in app.aliases:
            return app.key
    return None


def _title(v, _):
    s = T.title_slots(v)
    return s if s["title"] else None


NOT_A_TITLE = re.compile(r"^(?:it|this|that|something|anything|again|the movie|the film|the video|"
                         r"(?:the )?(?:next|previous|last|prior) (?:episode|one|part)|"
                         r"(?:a |some |any )?(?:game|games|music|song|songs|videos?|movies?|films?|shows?|series))$")


def _play(v, rules):
    """Like title, but not when what follows "play" is an app, the show that's on, or "the next one"."""
    s = T.title_slots(v)
    t = s["title"]
    if not t or t in T.CURRENT or NOT_A_TITLE.match(t) or _alias(t, rules):
        return None
    return s


CONVERTERS = {
    "text": lambda v, _: v.strip() or None,
    "seconds": lambda v, _: seconds_in(v),
    "time": _time,
    "number": lambda v, _: to_int(v),
    "amount": lambda v, _: T.amount(v),
    "direction": lambda v, _: {"louder": "up", "higher": "up", "up": "up", "quieter": "down", "softer": "down",
                               "lower": "down", "down": "down"}.get(v.strip()),
    "query": lambda v, _: T.query(v) or None,
    "title": _title,
    "play": _play,
    "episode_here": lambda v, _: T.episode_here(v),
    "continue": lambda v, _: T.continue_title(v),
    "app": _app,
    "alias": _alias,
}
CONVERTER_HELP = {
    "text": "the words as said",
    "seconds": "a duration in seconds ('thirty seconds', 'two minutes')",
    "time": "a position: a duration, or a bare number as minutes",
    "number": "a whole number ('twenty', '15')",
    "amount": "'a bit' = 3, 'a lot' = 15, or a number",
    "direction": "louder/higher -> up, quieter/softer/lower -> down",
    "query": "a search, without a leading 'some' / 'videos of'",
    "title": "title, season, episode, kind, from-the-start (use with \"*\")",
    "play": "like title, but no match for an app name, 'it' or 'the next one' (use with \"*\")",
    "episode_here": "season/episode of the show that's on, when no title is said (use with \"*\")",
    "continue": "a title, or nothing for 'the show' / 'where I left off'",
    "app": "an app from the app list, by name",
    "alias": "an app, only by one of its exact spoken names",
}


# ---- loading and checking ----

def _words_regex(words: list[str], anchored_start: bool) -> re.Pattern | None:
    words = [w.strip().lower() for w in words if w.strip()]
    if not words:
        return None
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    if anchored_start:
        return re.compile(rf"^(?:(?:{alts})(?:[,\s]+|$))+")
    return re.compile(rf"[,\s]*\b(?:{alts})\b")


def _check_template(where: str, do: dict, groups: set[str]):
    if not isinstance(do, dict) or do.get("type") not in A.ACTION_TYPES:
        raise RulesError(f"{where}: 'do' needs a known \"type\" ({', '.join(A.ACTION_TYPES)})")
    model = A.ACTION_TYPES[do["type"]]
    for key, val in do.items():
        if key not in model.model_fields and key != "*":
            raise RulesError(f"{where}: {do['type']} has no field {key!r} (fields: {', '.join(model.model_fields)})")
        m = PLACEHOLDER.match(val) if isinstance(val, str) else None
        if m:
            if m.group(1) not in groups:
                raise RulesError(f"{where}: {{{m.group(1)}}} isn't a named part of the pattern")
            if (m.group(2) or "text") not in CONVERTERS:
                raise RulesError(f"{where}: unknown converter {m.group(2)!r} ({', '.join(CONVERTERS)})")
    if not any(isinstance(v, str) and PLACEHOLDER.match(v) for v in do.values()):
        try:
            A.parse(do)
        except ValidationError as e:
            raise RulesError(f"{where}: {e.errors()[0]['msg']} ({e.errors()[0]['loc']})") from None


def build(raw: dict, source: str) -> Rules:
    """Check everything and compile; raises RulesError with a readable message."""
    if not isinstance(raw, dict):
        raise RulesError("the rules file must be a JSON object")
    norm = raw.get("normalize", {})
    fixes = []
    for i, f in enumerate(norm.get("fixes", [])):
        try:
            fixes.append((re.compile(f["from"]), str(f.get("to", ""))))
        except (re.error, KeyError, TypeError) as e:
            raise RulesError(f"clean-up fix {i + 1}: {e}") from None

    apps = {}
    for a in raw.get("apps", []):
        key = str(a.get("key", ""))
        if not re.fullmatch(r"[a-z0-9_]{1,30}", key):
            raise RulesError(f"app {key!r}: the key must be lowercase letters, digits or _")
        if not PACKAGE.match(a.get("package", "")) or not ACTIVITY.match(a.get("activity", "")):
            raise RulesError(f"app {key}: package or activity looks wrong")
        aliases = tuple(str(x).strip().lower() for x in a.get("aliases", []) if str(x).strip())
        if not aliases:
            raise RulesError(f"app {key}: give it at least one spoken name")
        apps[key] = App(key, str(a.get("label") or key), a["package"], a["activity"], aliases)
    if not apps:
        raise RulesError("the app list is empty")

    hooks = raw.get("hooks", {})
    for name, needs in (("youtube", "{id}"), ("twitch", "{channel}"), ("stremio", "{id}")):
        h = hooks.get(name)
        if not h or h.get("app") not in apps:
            raise RulesError(f"hook {name}: its app must be one of the apps ({', '.join(apps)})")
        for k in ("link", "link_episode"):
            link = h.get(k)
            if link is None and k == "link_episode":
                continue
            if not isinstance(link, str) or needs not in link or not link.startswith(LINK_SCHEMES):
                raise RulesError(f"hook {name}: {k} must start with a link scheme and contain {needs}")

    rules = Rules(raw=raw, source=source, fillers=_words_regex(norm.get("fillers", []), True) or re.compile("^$"),
                  remove_words=_words_regex(norm.get("remove_words", []), False), fixes=fixes, phrases={},
                  patterns=[], question="", min_confidence=0.3, intents={}, fallback_intent="play_title",
                  title_question="", title_candidates=4, apps=apps, hooks=hooks)

    for i, p in enumerate(raw.get("phrases", [])):
        pid = str(p.get("id") or f"phrase {i + 1}")
        _check_template(f"phrase {pid}", p.get("do"), set())
        for s in p.get("say", []):
            s = str(s).strip().lower()
            if s:
                rules.phrases[s] = (p["do"], pid)

    seen = set()
    for i, p in enumerate(raw.get("patterns", [])):
        pid = str(p.get("id") or f"pattern {i + 1}")
        if pid in seen:
            raise RulesError(f"pattern {pid}: the id is used twice")
        seen.add(pid)
        try:
            rx = re.compile(p.get("match", ""))
        except re.error as e:
            raise RulesError(f"pattern {pid}: the regex doesn't compile ({e})") from None
        _check_template(f"pattern {pid}", p.get("do"), set(rx.groupindex))
        rules.patterns.append(Pattern(pid, p.get("stage", "grammar"), rx, p["do"], p.get("note", "")))

    m = raw.get("model", {})
    rules.question = str(m.get("question") or "TV voice command: what should the TV do?")
    rules.min_confidence = float(m.get("min_confidence", 0.3))
    if not 0 <= rules.min_confidence <= 1:
        raise RulesError("model: min_confidence must be between 0 and 1")
    rules.min_ratio = float(m.get("min_ratio", 1.0))
    if rules.min_ratio < 1:
        raise RulesError("model: min_ratio (top answer over the second) must be 1 or more")
    for key, it in m.get("intents", {}).items():
        it = it if isinstance(it, dict) else {"describe": str(it)}
        if not it.get("enabled", True):
            continue
        if not re.fullmatch(r"[a-z0-9_]{1,40}", key):
            raise RulesError(f"intent {key!r}: lowercase letters, digits or _")
        if not it.get("describe"):
            raise RulesError(f"intent {key}: describe what it means, d1 decides on that text")
        if key not in BUILTIN_INTENTS:
            _check_template(f"intent {key}", it.get("do"), {"text"})
        rules.intents[key] = it
    if "none" not in rules.intents:
        rules.intents["none"] = {"describe": "not a TV command"}
    if len(rules.intents) < 2 or len(rules.intents) > 60:
        raise RulesError("model: between 2 and 60 intents")
    rules.fallback_intent = str(m.get("offline_fallback", "play_title"))
    rules.title_question = str(m.get("title_question") or "Which title does the user most likely mean?")
    rules.title_candidates = max(2, min(8, int(m.get("title_candidates", 4))))
    rules.counts = {"phrases": len(rules.phrases), "patterns": len(rules.patterns), "intents": len(rules.intents),
                    "apps": len(rules.apps)}
    return rules


def defaults_raw() -> dict:
    return json.loads(DEFAULTS_FILE.read_text())


def load() -> Rules:
    """The user's rules if present and valid, else the defaults (a broken file never stops the agent)."""
    if USER_FILE.exists():
        return build(json.loads(USER_FILE.read_text()), str(USER_FILE))
    return build(defaults_raw(), "defaults")


def load_safe() -> tuple[Rules, str | None]:
    """load(), but an invalid rules.json falls back to the defaults and returns the error to show."""
    try:
        return load(), None
    except (RulesError, ValueError, OSError) as e:
        return build(defaults_raw(), "defaults (rules.json has an error)"), f"{USER_FILE.name}: {e}"


def save(raw: dict) -> Rules:
    rules = build(raw, str(USER_FILE))      # raises before anything is written
    USER_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = USER_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(USER_FILE)
    return rules


def reset() -> Rules:
    USER_FILE.unlink(missing_ok=True)
    return build(defaults_raw(), "defaults")
