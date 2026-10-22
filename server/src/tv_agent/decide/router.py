"""Transcript -> one typed action, following the rules (rules.py; editable in the panel's Decisions tab).

1. Clean-up: fillers at the start, words removed anywhere, fixes for common mishearings. Always on.
2. Then the steps, in the order set in the Decisions tab (`decide_steps`), each of them switchable (`decide_off`):
   phrases   an exact sentence -> its action ("pause"), including the ones you taught
   patterns  pattern rules, in order: the first whose regex matches and whose parts convert wins
   llm       an LLM (llm.py, with an OpenRouter key) picks the action; its "not a command" is final
   d1        d1 picks one of the intents and rules pull out the title, numbers…; below its confidence threshold the
             next step gets a try. Local, hosted or either first (`d1_where`, d1.py)
   A step that can't be reached passes to the next. When no model answered (off or unreachable), what's left is
   treated as the rules' offline intent (a title to play, or nothing).
3. Two commands in one ("play Severance episode 2 and go to the middle"): when one side is a control command the
   rules know, the other side goes through the steps and both run, one after the other.
Every decision carries a trace of the steps it went through, for the panel's diagram.
"""
import logging
import re
from dataclasses import dataclass, field

import httpx
from pydantic import ValidationError

from .. import actions as A
from . import text as T
from .d1 import D1
from .numbers import NUMBER, TENS, UNITS, seconds_in, to_int
from .rules import BUILTIN_INTENTS, LANGUAGE_NAMES, NoMatch, Rules


@dataclass
class Decision:
    action: A.Action
    via: str                          # "phrase", "pattern", "llm", "d1", "split" (two commands) or "none"
    confidence: float = 1.0
    rule: str = ""                    # phrase group or pattern id; the intent for d1
    trace: list[dict] = field(default_factory=list)


log = logging.getLogger(__name__)
STEPS = ("phrases", "patterns", "llm", "d1")
STAGE = {"phrases": "phrase", "patterns": "pattern", "llm": "llm", "d1": "model"}    # their names in the trace
# What one side of "… and …" must be, as the rules decide it, for the sentence to be two commands
CONTROL = {"transport", "volume", "tracks", "skip", "auto_skip", "binge", "timer", "replay", "home", "next_episode",
           "show_list", "help", "info"}
SPLIT = re.compile(r",?\s+(?:and then|and|then|y luego|y despu[eé]s|y)\s+")
# What follows an "and" / "y" that's still the same command: "an hour and a half", "treinta y cinco"
_NUMBER_WORDS = "|".join(sorted((w for w in [*UNITS, *TENS, "hundred"] if w not in ("a", "an", "un", "una", "oh")),
                                key=len, reverse=True))
SAME_COMMAND = re.compile(rf"(?:\d|(?:{_NUMBER_WORDS}|a half|half|a quarter|medio|media|un cuarto)\b)")


def normalize(text: str, rules: Rules) -> str:
    t = text.lower().strip()
    t = re.sub(r"[“”\"!?¿¡]", "", t)
    t = re.sub(r"\s+", " ", t).rstrip(".").strip()
    for rx, to in rules.fixes:
        t = rx.sub(to, t)
    said = t
    t = rules.fillers.sub("", t)
    if rules.remove_words is not None:
        t = rules.remove_words.sub("", t)
    t = t.strip(" ,.")
    return t or ("please" if "please" in said else "")   # a lone "please" is usually a misheard "pause"


def match_phrase(t: str, rules: Rules, trace: list) -> tuple[A.Action, str, str] | None:
    if t in rules.phrases:
        do, pid = rules.phrases[t]
        trace.append({"stage": "phrase", "rule": pid, "matched": True})
        return A.parse(do), "phrase", pid
    trace.append({"stage": "phrase", "matched": False})
    return None


def match_rules(t: str, rules: Rules, trace: list) -> tuple[A.Action, str, str] | None:
    """Fixed phrases, then pattern rules in order. (action, via, rule id) or None."""
    return match_phrase(t, rules, trace) or match_patterns(t, rules, trace)


def match_patterns(t: str, rules: Rules, trace: list) -> tuple[A.Action, str, str] | None:
    for p in rules.patterns:
        m = p.regex.match(t)
        if not m:
            continue
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        try:
            action = A.parse(rules.fill(p.do, groups))
        except NoMatch:
            trace.append({"stage": "pattern", "rule": p.id, "matched": False, "why": "a part didn't convert",
                          "parts": groups})
            continue
        except ValidationError as e:
            log.warning("pattern %s made an invalid action: %s", p.id, e.errors()[0]["msg"])
            trace.append({"stage": "pattern", "rule": p.id, "matched": False, "why": "invalid action"})
            continue
        trace.append({"stage": "pattern", "rule": p.id, "group": p.stage, "matched": True, "parts": groups})
        return action, "pattern", p.id
    trace.append({"stage": "pattern", "matched": False})
    return None


def from_intent(intent: str, t: str, rules: Rules) -> A.Action | None:
    """Slots for a d1 intent: built-in extraction, or a custom intent's template over the whole sentence."""
    it = rules.intents.get(intent, {})
    if intent not in BUILTIN_INTENTS or intent == "none":
        try:
            return A.parse(rules.fill(it.get("do", {}), {"text": t})) if it.get("do") else None
        except (NoMatch, ValidationError):
            return None
    if intent in ("pause", "resume", "stop"):
        return A.Transport(op=intent)
    if intent in ("skip_forward", "skip_back"):
        return A.Transport(op="forward" if intent == "skip_forward" else "back", seconds=seconds_in(t))
    if intent == "restart":
        return A.Transport(op="seek_to", seconds=0)
    if intent == "seek_to":
        if re.search(r"\b(?:middle|half ?way)\b", t):
            return A.Transport(op="seek_to", percent=50)
        at = re.sub(r"^.*?\b(?:to|at)\s+(?:minute\s+)?", "", t)
        secs = seconds_in(at)
        if secs is None and (n := to_int(at)) is not None:
            secs = n * 60
        return A.Transport(op="seek_to", seconds=secs) if secs is not None else None
    if intent == "previous_episode":
        return A.NextEpisode(offset=-1)
    if intent == "volume_set":
        m = re.search(rf"({NUMBER})\s*(?:percent|%)?$", t)
        n = to_int(m.group(1)) if m else None
        return A.Volume(op="set", amount=n) if n is not None and n <= 100 else None
    if intent in ("volume_up", "volume_down"):
        m = re.search(r"\b(?:by\s+)?(a lot|a (?:little )?bit|a little|slightly|much|\d+|" + NUMBER + r")\b", t)
        return A.Volume(op=intent.removeprefix("volume_"), amount=T.amount(m.group(1)) if m else None)
    if intent in ("mute", "unmute"):
        return A.Volume(op=intent)
    if intent == "binge":
        return A.Binge(on=not re.search(r"\b(?:off|disable|stop|no|cancel|deactivate)\b", t))
    if intent == "sleep_timer":
        if re.search(r"\bcancel\b|\bno (?:sleep )?timer\b|\bkeep (?:playing|going)\b", t):
            return A.Timer(cancel=True)
        off = re.search(r"\b(?:turn|switch|shut|power)(?: it| the tv)? (?:off|down)\b|\bsleep\b", t)
        secs = seconds_in(t)
        return A.Timer(minutes=max(1, round(secs / 60)) if secs else None, then="off" if off else "stop")
    if intent in ("subtitles", "audio_language"):
        lang = next((code for name, code in sorted(LANGUAGE_NAMES.items(), key=lambda kv: -len(kv[0]))
                     if re.search(rf"\b{name}\b", t)), None)
        if intent == "subtitles":
            if lang:
                return A.Tracks(subtitles=lang)
            return A.Tracks(subtitles="off" if re.search(r"\b(?:off|no|hide|disable|remove)\b", t) else "on")
        if re.search(r"\boriginal\b", t):
            return A.Tracks(audio="original")
        return A.Tracks(audio=lang) if lang else None
    if intent == "replay":
        return A.Replay(seconds=seconds_in(t) or 10)
    if intent in ("show_recent", "show_new"):
        return A.ShowList(list="recent" if intent == "show_recent" else "new")
    if intent == "pick_number":
        m = re.search(rf"\b(\d+|{NUMBER}|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\b", t)
        n = to_int(m.group(1)) if m else None
        return A.Pick(number=n) if n and 1 <= n <= 20 else None
    if intent == "latest_episode":
        rest = re.sub(r"\b(?:the )?(?:latest|newest|most recent|new|last)(?: released)? episode(?: of| from| in)?\b", " ", t)
        s = T.title_slots(rest)
        return A.PlayTitle(title=s["title"], latest=True, kind="series") if s["title"] else None
    if intent == "library":
        op = ("remove" if re.search(r"\b(?:remove|delete|take off|drop)\b", t) else
              "unwatched" if re.search(r"\b(?:unwatched|not (?:watched|seen)|unseen)\b", t) else
              "watched" if re.search(r"\b(?:watched|seen)\b", t) else "add")
        rest = re.sub(r"\b(?:add|save|put|remove|delete|take|drop|mark|set|flag|as|to|from|off|out of|in|into|on|my|our|"
                      r"the library|library|watch ?list|list|unwatched|not watched|watched|seen|unseen)\b", " ", t)
        s = T.title_slots(re.sub(r"\s+", " ", rest).strip())
        title = None if (s["title"] or "") in T.CURRENT or re.match(r"^(?:this|that|it)\b", s["title"] or "") else s["title"]
        return A.LibraryEdit(op=op, title=title, season=s["season"], episode=s["episode"], kind=s["kind"])
    if intent == "help":
        return A.Help()
    if intent == "info":
        m = re.search(r"\b(?:in|of|with|into)\s+(.+?)(?:\s+(?:that\s+)?(?:am i on|are we on|i watched|we watched))?$", t)
        return A.Info(title=T.continue_title(m.group(1)) if m else None)
    if intent == "skip_segment":
        if re.search(r"\b(?:recap|previously|summary)\b", t):
            return A.Skip(what="recap")
        if re.search(r"\b(?:credits|outro|ending)\b", t) and not re.search(r"\bopening\b", t):
            return A.Skip(what="credits")
        return A.Skip(what="intro")
    if intent in ("play_title", "open_in_stremio"):
        s = T.title_slots(t)
        if (s["title"] or "") in T.CURRENT:
            if intent == "play_title" and (s["season"] is not None or s["episode"] is not None):
                return A.PlayTitle(**{**s, "title": None})     # an episode of the show that's on
            return None
        if intent == "open_in_stremio":
            return A.OpenInStremio(**{k: s[k] for k in ("title", "season", "episode", "kind")})
        return A.PlayTitle(**s)
    if intent == "continue_watching":
        rest = re.sub(r"^(?:continue|resume|keep watching|carry on with|carry on|finish|go on with)\s*", "", t)
        return A.ContinueWatching(title=T.continue_title(T.clean_title(rest)))
    if intent == "next_episode":
        return A.NextEpisode()
    if intent == "youtube":
        q = T.clean_title(re.sub(r"\b(?:on|in|from)?\s*(?:youtube|you tube)\b", "", t))
        hook_app = rules.hooks["youtube"]["app"]
        return A.YouTube(query=q) if q else A.OpenApp(app=hook_app)
    if intent == "twitch":
        ch = T.clean_title(re.sub(r"\b(?:on|in|from)?\s*twitch\b", "", t))
        return A.Twitch(channel=ch) if ch else A.OpenApp(app=rules.hooks["twitch"]["app"])
    if intent == "open_app":
        app = rules.find_app(T.clean_title(t))
        return A.OpenApp(app=app.key) if app else None
    if intent == "home":
        return A.Home()
    if intent == "transport":
        if re.search(r"\b(?:pause|hold|wait)\b", t):
            return A.Transport(op="pause")
        if re.search(r"\b(?:stop)\b", t):
            return A.Transport(op="stop")
        if re.search(r"\b(?:back|rewind)\b", t):
            return A.Transport(op="back", seconds=seconds_in(t))
        if re.search(r"\b(?:skip|forward|ahead)\b", t):
            return A.Transport(op="forward", seconds=seconds_in(t))
        return A.Transport(op="resume")
    if intent == "volume":
        if re.search(r"\bunmute\b|\bsound (?:on|back)\b", t):
            return A.Volume(op="unmute")
        if re.search(r"\bmute\b|\bsilen", t):
            return A.Volume(op="mute")
        up = re.search(r"\b(?:up|louder|raise|more|increase|can'?t hear|too (?:quiet|low|soft))\b", t)
        return A.Volume(op="up" if up else "down", amount=T.amount(t))
    return None


class Router:
    def __init__(self, d1: D1, rules: Rules, llm=None, context=None, steps=STEPS, off=()):
        self.d1, self.rules = d1, rules
        self.llm = llm                  # llm.Llm, or None (no OpenRouter key)
        self.context = context          # async (text) -> dict for the LLM: what's playing, candidate titles, apps
        self.steps = [s for s in steps if s in STEPS] + [s for s in STEPS if s not in steps]
        self.off = set(off)             # the steps switched off (the panel's Decisions tab changes both live)

    async def decide(self, text: str) -> Decision:
        rules = self.rules                      # one consistent set even if a reload lands meanwhile
        t = normalize(text, rules)
        trace = [{"stage": "clean", "text": t}]
        if not t:
            return Decision(A.Unclear(heard=text), "none", 0.0, "", trace)
        d = await self._two(t, rules, trace) or await self._steps(text, t, rules, trace)
        help_in = A.Help(lang="es" if T.spanish(text) else "en")         # the list in the language it was asked in
        if d.action.type == "help" and d.action.lang is None:
            d.action = help_in
        elif d.action.type == "sequence" and any(x.type == "help" for x in d.action.steps):
            d.action = A.Sequence(steps=[help_in if x.type == "help" else x for x in d.action.steps])
        return d

    def _rules_only(self, t: str, rules: Rules) -> tuple[A.Action, str, str] | None:
        trace = []
        return (("phrases" not in self.off and match_phrase(t, rules, trace))
                or ("patterns" not in self.off and match_patterns(t, rules, trace)) or None)

    async def _two(self, t: str, rules: Rules, trace: list) -> Decision | None:
        """"play X and go to the middle", "turn it down and put on the news": two commands, when one side is a control
        command the rules know (so "pride and prejudice" or "tom and jerry" stay one title). The other side goes
        through every step."""
        if "phrases" not in self.off and t in rules.phrases:
            return None                         # a fixed phrase (or a taught one) with an "and" in it
        for m in SPLIT.finditer(t):
            left, right = t[:m.start()].strip(" ,"), t[m.end():].strip(" ,")
            if not left or not right or SAME_COMMAND.match(right):
                continue
            for known, other in ((right, left), (left, right)):
                hit = self._rules_only(normalize(known, rules), rules)     # each side gets its own clean-up
                if not hit or hit[0].type not in CONTROL:
                    continue
                part = [{"stage": "split", "parts": [left, right], "known": known, "rule": hit[2]}]
                said = normalize(other, rules)
                d = await self._steps(other, said, rules, part)
                if d.action.type in ("none", "sequence") or d.via == "none" or (
                        # a bare name ("skip the intro and the recap") isn't a title to play
                        d.action.type == "play_title" and not T.PLAY_VERBS.match(said)
                        and d.action.season is None and d.action.episode is None):
                    continue
                trace += part
                steps = [d.action, hit[0]] if other == left else [hit[0], d.action]
                return Decision(A.Sequence(steps=steps), "split", d.confidence, f"{d.rule} + {hit[2]}", trace)
        return None

    async def _steps(self, text: str, t: str, rules: Rules, trace: list) -> Decision:
        unsure = None                           # d1's answer when it wasn't sure enough to act
        for step in self.steps:
            if step in self.off or (step == "llm" and self.llm is None):
                trace.append({"stage": STAGE[step], "off": True, "why": "off" if step in self.off else "no key"})
                continue
            if step == "phrases" and (hit := match_phrase(t, rules, trace)):
                return Decision(hit[0], hit[1], 1.0, hit[2], trace)
            if step == "patterns" and (hit := match_patterns(t, rules, trace)):
                return Decision(hit[0], hit[1], 1.0, hit[2], trace)
            if step == "llm":
                context = await self.context(text) if self.context else {}
                reached, action = await self.llm.answer(text, context)
                trace.append({"stage": "llm", "model": self.llm.model, "action": action.type if action else "none",
                              "reached": reached})
                if reached:                     # its "not a command" is final: the d1 after it guesses worse
                    return Decision(action or A.Unclear(heard=text), "llm", 0.9 if action else 0.0, "llm", trace)
            if step == "d1" and (d := await self._ask_d1(text, t, rules, trace)):
                if d.action.type != "none":
                    return d
                unsure = d
        if unsure:
            return unsure
        # No model answered (off or unreachable): the rules' offline intent ("play it as a title"), if any
        action = from_intent(rules.fallback_intent, t, rules) if rules.fallback_intent != "none" else None
        trace.append({"stage": "fallback", "intent": rules.fallback_intent, "accepted": action is not None})
        return Decision(action or A.Unclear(heard=text), "none", 0.0, rules.fallback_intent, trace)

    async def _ask_d1(self, text: str, t: str, rules: Rules, trace: list) -> Decision | None:
        """d1's decision ("none" when it isn't sure enough), or None when no d1 answered."""
        options = {k: v["describe"] for k, v in rules.intents.items()}
        where = getattr(self.d1, "where", "local-first")
        try:
            pick = await self.d1.choice(t, rules.question, options)
        except (httpx.HTTPError, KeyError) as e:
            log.warning("d1 unavailable (%s)", type(e).__name__)
            trace.append({"stage": "model", "available": False, "where": where})
            return None
        top = sorted(pick.probabilities.items(), key=lambda kv: -kv[1])[:3]
        step = {"stage": "model", "intent": pick.key, "confidence": round(pick.confidence, 3),
                "threshold": rules.min_confidence, "top": [[k, round(p, 3)] for k, p in top],
                "source": getattr(self.d1, "last_source", "local"), "where": where}
        trace.append(step)
        ratio = top[0][1] / max(top[1][1], 1e-9) if len(top) > 1 else float("inf")
        action = None
        if pick.key != "none" and pick.confidence >= rules.min_confidence and ratio >= rules.min_ratio:
            action = from_intent(pick.key, t, rules)
        step["accepted"] = action is not None
        return Decision(action or A.Unclear(heard=text), "d1", pick.confidence, pick.key, trace)
