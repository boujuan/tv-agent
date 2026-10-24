"""Chains, read: "pon el capítulo 5 de la segunda temporada de The Bear y vete al minuto 12" is a play and then a
jump, one after the other (actions.Sequence, run by tv_agent/chain.py). What may be chained is fixed
(actions.chain_problem): one step that plays a title or an episode, then one or two that act on what it plays (a
jump to a time, a scene or past the intro, the audio or subtitle language, the volume, a pause), in the order said.

Rules first. The sentence is cut at its joins ("and", "then", "y luego", a comma…), and every part after the first
must be a command the rules know on its own (fixed phrases and pattern rules, no model); the first part then goes
through the decision steps like any command. Endings that ride on a play are cut off the same way: "… con subtítulos
en español", "… with Spanish subtitles", "… desde el minuto 12", "… in English".

A model only proposes. When the rules find no chain but the sentence sounds like one, the LLM is asked for its
steps as JSON in a strict shape (STEPS_SCHEMA). An action or field outside the chain's list, a step after the play
whose words weren't said, or steps actions.chain_problem refuses reject the whole chain: the first step runs alone
when it holds up by itself, with a note saying what was left out; else nothing happens.

Nothing unclear is chained: a play part no step is sure of (the bar of a single command), a bare name with no play
verb or episode, a part the rules can't place.
"""
import itertools
import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from .. import actions as A
from .llm import ACTIONS, SCHEMA, SLOTS, SYSTEM, to_action
from .numbers import NUMBER, TENS, UNITS
from .rules import LANGUAGE_NAMES

# Where one command ends and the next begins
JOIN = re.compile(r"(?:\s*[,;]\s*|\.\s+|\s+)(?:and then|and|then|y luego|y despu[eé]s|luego|despu[eé]s|y)\s+"
                  r"|\s*[,;]\s*|\.\s+")
# What follows an "and" / "y" that's still the same command: "an hour and a half", "treinta y cinco"
_NUMBER_WORDS = "|".join(sorted((w for w in [*UNITS, *TENS, "hundred"] if w not in ("a", "an", "un", "una", "oh")),
                                key=len, reverse=True))
SAME_COMMAND = re.compile(rf"(?:\d|(?:{_NUMBER_WORDS}|a half|half|a quarter|medio|media|un cuarto)\b)")
# Commands the rules know that make a part a command of its own, not more of a title ("pride and prejudice")
AFTER = {"transport", "volume", "tracks", "skip", "auto_skip", "binge", "timer", "replay", "home", "next_episode",
         "show_list", "help", "info", "find_scene"}
MOST_JOINS = 6                      # the joins tried, from the start (each part is a few regexes: microseconds)

_LANG = "|".join(sorted(map(re.escape, LANGUAGE_NAMES), key=len, reverse=True))
_SUBS = r"(?:subtitles?|subs|subt[ií]tulos?)"
def _audio_in(m):
    return f"audio in {m['lang']}"


def _original(m):
    return "original audio"


# Endings that ride on a play, as the command each one is (rules decide it like any other part). Tried from the end.
ENDINGS = [
    # "… with Spanish subtitles", "… con subtítulos en español", "… with subtitles"
    (re.compile(rf"\s+(?:with|con)\s+(?:the\s+|los\s+|unos\s+)?(?P<say>(?:(?:{_LANG})\s+)?{_SUBS}"
                rf"(?:\s+(?:in|en)\s+(?:{_LANG}))?)$"), lambda m: m["say"]),
    # "… with the audio in English", "… con el audio en inglés", "… with English audio"
    (re.compile(rf"\s+(?:with|con)\s+(?:the\s+|el\s+)?(?P<say>(?:(?:{_LANG})\s+)?audio(?:\s+(?:in|en)\s+"
                rf"(?:{_LANG}|original))?)$"), lambda m: m["say"]),
    # "… in English", "… en español"
    (re.compile(rf"\s+(?:in|en)\s+(?P<lang>{_LANG})$"), _audio_in),
    # "… en versión original", "… in the original version", "… in VO"
    (re.compile(r"\s+(?:in|en)\s+(?:the\s+|la\s+)?(?:original(?:\s+(?:version|language))?|versi[oó]n original|"
                r"v\.?\s?o\.?)$"), _original),
    # "… from minute 12", "… desde el minuto 12", "… at 12 minutes"
    (re.compile(rf"\s+(?:from|at|starting at)\s+(?:the\s+)?(?P<at>minute\s+(?:\d+|{NUMBER})|(?:\d+|{NUMBER})\s+"
                r"minutes?(?:\s+in)?)$"), lambda m: f"go to {m['at']}"),
    (re.compile(rf"\s+(?:desde|a partir del?|en)\s+(?:el\s+)?(?P<at>minuto\s+(?:\d+|{NUMBER}))$"),
     lambda m: f"ve al {m['at']}"),
]


# "subtítulos en inglés", "audio in Spanish": the language belongs to the words before it, not to a play
OF_TRACKS = re.compile(rf"\b(?:{_SUBS}|audio|sound|sonido|voices|voces)$")


def clean(text: str) -> str:
    """The sentence as said: lower case, no quotes, no final full stop. It's cut before the clean-up, which then reads
    each part on its own: its fixes match at a part's start ("vete al minuto 12" is a jump only there)."""
    t = re.sub(r"[“”\"!?¿¡]", "", text.lower().strip())
    return re.sub(r"\s+", " ", t).rstrip(". ").strip()


def cuts(t: str) -> list[list[str]]:
    """Ways to cut a sentence at its joins into a first part and one or two after it, the most parts first; the
    whole sentence last (its endings may still be steps). A part starting with a number belongs to the one before."""
    joins = [(m.start(), m.end()) for m in JOIN.finditer(t)][:MOST_JOINS]
    out = []
    for n in (2, 1):
        for picked in itertools.combinations(joins, n):
            parts, at = [], 0
            for start, end in picked:
                parts.append(t[at:start].strip(" ,;."))
                at = end
            parts.append(t[at:].strip(" ,;."))
            if all(parts) and not any(SAME_COMMAND.match(p) for p in parts[1:]):
                out.append(parts)
    return out + [[t]]


def endings(part: str) -> tuple[str, list[str]]:
    """A play part without the endings that ride on it, and those endings as commands, in the order said (three at
    most, one more than a chain takes, so a title doesn't keep the third): "pon oppenheimer desde el principio con
    subtítulos en español" -> ("pon oppenheimer desde el principio", ["subtítulos en español"])."""
    found: list[str] = []
    for _ in range(3):
        for rx, command in ENDINGS:
            if (m := rx.search(part)) and m.start() > 0 and not (
                    command in (_audio_in, _original) and OF_TRACKS.search(part[:m.start()])):
                found.insert(0, command(m))
                part = part[:m.start()].strip(" ,")
                break
        else:
            break
    return part, found


def settled(action: A.Action) -> A.Action:
    """A step as a chain means it: "… with subtitles" turns them on (a toggle right after a start may turn off the
    ones the start turned on)."""
    if isinstance(action, A.Tracks) and action.subtitles == "toggle":
        return action.model_copy(update={"subtitles": "on"})
    return action


# ---- the LLM's proposal ----

FIRST = {"play_title", "continue_watching", "next_episode"}
FOLLOW = {"seek_to", "seek_percent", "start_over", "find_scene", "skip_intro", "skip_recap", "subtitles_on",
          "subtitles_off", "subtitles", "audio", "volume_up", "volume_down", "volume_set", "mute", "unmute", "pause"}
# The fields each step of a chain may fill; any other must be null (a play with "seconds" merged two steps into one)
FIELDS = {"play_title": {"title", "season", "episode", "episode_name"}, "continue_watching": {"title"},
          "seek_to": {"seconds"}, "seek_percent": {"number"}, "find_scene": {"query"}, "subtitles": {"language"},
          "audio": {"language"}, "volume_set": {"number"}, "volume_up": {"number"}, "volume_down": {"number"}}
# Words that must have been said for a step after the play: the model may not add one
SAID = {
    "time": r"\b(?:minutes?|minutos?|min|hours?|horas?|seconds?|segundos?|middle|mitad|half|medio|start|beginning|"
            r"principio|inicio|comienzo|top|percent|por ciento)\b|\d+:\d\d|\d+\s?%",
    "find_scene": r"\b(?:scenes?|escenas?|parts?|partes?|moments?|momentos?|bits?|trozos?|where|when|donde|cuando)\b",
    "skip_intro": r"\b(?:intro|introducci[oó]n|opening|theme|title sequence|cabecera|sinton[ií]a|entradilla)\b",
    "skip_recap": r"\b(?:recap|previously|resumen|anteriormente)\b",
    "subtitles": rf"\b(?:{_SUBS}|captions|cc)\b",
    "audio": rf"\b(?:audio|sound|sonido|voices|voces|original|v\.?\s?o|dubbed|doblad[oa]|{_LANG})\b",
    "volume": r"\b(?:volume|volumen|louder|quieter|loud|alto|bajo|mute|silencio|sound|sonido|turn (?:it )?(?:up|down)|"
              r"s[uú]bel[oa]|b[aá]jal[oa])\b",
    "pause": r"\b(?:pause|pausa|pausado|stop|para|pare|det[eé]n|hold|wait|espera)\b",
}
SAID_BY = {"seek_to": "time", "seek_percent": "time", "start_over": "time", "subtitles_on": "subtitles",
           "subtitles_off": "subtitles", "volume_up": "volume", "volume_down": "volume", "volume_set": "volume",
           "mute": "volume", "unmute": "volume"}

STEPS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["steps"],
                "properties": {"steps": {"type": "array", "minItems": 1, "maxItems": 5, "items": SCHEMA}}}
STEPS_SYSTEM = ("You turn one spoken command for a TV voice agent into its actions, in the order said. Answer only "
                "with the JSON object.\n" + SYSTEM.split("\n", 1)[1] + """

The command may ask for a few things one after the other. Answer "steps": its actions in the order said, each in the
shape above (every field, null when unused). Most commands are one step. More than one only when it first plays
something (play_title, continue_watching or next_episode) and then acts on what that plays: seek_to, seek_percent,
start_over, find_scene, skip_intro, skip_recap, subtitles_on, subtitles_off, subtitles, audio, volume_up,
volume_down, volume_set, mute, unmute or pause. Keep each step's own fields in it (a time is its own seek_to step, not
part of the play). A moment described in words ("when…", "the part where…") is find_scene, never seek_to: seek_to
only for a time that was said. Never add a step that wasn't asked for.

"pon el capítulo 5 de la segunda temporada de the bear y vete al minuto 12" -> {"steps":[{"action":"play_title","title":"The Bear","season":2,"episode":5},{"action":"seek_to","seconds":720}]}
"put inception on and take me to when he spins the top" -> {"steps":[{"action":"play_title","title":"Inception"},{"action":"find_scene","query":"Cobb spins the top"}]}
"quiero ver amélie en versión original con subtítulos en inglés" -> {"steps":[{"action":"play_title","title":"Amélie"},{"action":"audio","language":"original"},{"action":"subtitles","language":"english"}]}
"turn it up a bit" -> {"steps":[{"action":"volume_up"}]}""")


async def ask(llm, said: str, context: dict, timeout: float = 4.0) -> dict | None:
    """The LLM's steps for a sentence, in STEPS_SCHEMA's shape (checked by read(), not trusted)."""
    data = await llm._post({
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "tv_steps", "strict": True, "schema": STEPS_SCHEMA}},
        "messages": [{"role": "system", "content": STEPS_SYSTEM},
                     {"role": "user", "content": json.dumps({"said": said, **context}, ensure_ascii=False)}]},
        timeout)
    llm.last_usage = data.get("usage") or {}
    content = data["choices"][0]["message"]["content"]
    answer = json.loads(content) if isinstance(content, str) else content
    return answer if isinstance(answer, dict) else None


@dataclass
class Reading:
    action: A.Action | None              # the chain, the one action answered, or the first step alone (problem)
    problem: str = ""                    # why the steps aren't a chain
    steps: tuple[str, ...] = ()          # the actions answered, for the trace


def read(answer: dict | None, said: str, candidates: list[str], rules) -> Reading:
    """The LLM's answer checked: one step is one action, read as any LLM answer is (llm.to_action); several must
    be a chain in every detail, or the whole chain is rejected (the first step alone, if it holds up by itself)."""
    steps = answer.get("steps") if isinstance(answer, dict) and set(answer) == {"steps"} else None
    if not isinstance(steps, list) or not steps or not all(isinstance(s, dict) for s in steps):
        return Reading(None, "not the shape asked for")
    kinds = tuple(str(s.get("action")) for s in steps)
    if len(steps) == 1:
        action = to_action(steps[0], said, candidates, rules)
        merged = {k for k in ("seconds", "number", "language", "query") if steps[0].get(k) not in (None, "")}
        if action is not None and steps[0].get("action") in FIRST and merged:     # a step folded into the play
            return Reading(action, f"the play came with {', '.join(sorted(merged))}", kinds)
        return Reading(action, steps=kinds)
    out: list[A.Action] = []
    problem = ""
    for n, step in enumerate(steps, 1):
        kind = step.get("action")
        if extra := set(step) - {"action", *SLOTS}:
            problem = f"step {n} has a field the chain doesn't know ({', '.join(sorted(extra))})"
        elif kind not in ACTIONS:
            problem = f"step {n} is an action the chain doesn't know ({kind})"
        elif kind not in (FIRST if n == 1 else FOLLOW):
            problem = (f"a chain starts by playing something, not {kind}" if n == 1 else
                       f"{kind.replace('_', ' ')} can't follow a play")
        elif unused := {k for k, v in step.items() if k != "action" and v not in (None, "")} - FIELDS.get(kind, set()):
            problem = f"step {n} ({kind}) has {', '.join(sorted(unused))}, which it doesn't take"
        elif n > 1 and not re.search(SAID[SAID_BY.get(kind, kind)], said.lower()):
            problem = f"step {n} ({kind.replace('_', ' ')}) wasn't asked for"
        elif (action := to_action(step, said, candidates, rules)) is None:
            problem = f"step {n} ({kind}) doesn't hold up"
        else:
            out.append(action)
            continue
        break
    if not problem:
        if len(out) > 3:
            problem = "more than three steps"
        else:
            try:
                return Reading(A.Sequence(steps=out), steps=kinds)
            except ValidationError as e:
                problem = str(e.errors()[0]["msg"]).removeprefix("Value error, ")
    return Reading(out[0] if out else None, problem, kinds)


def sounds_chained(t: str) -> bool:
    """Whether a sentence the rules didn't chain may still be a chain, worth the LLM's look: something played, then
    after a join or an ending, words of a step that can follow."""
    m = JOIN.search(t) or re.search(r"\s(?:with|con|from|desde|at|in|en|a partir)\s", t)
    if not m or not re.search(r"\b(?:play|put|pon|ponme|watch|ver|start|empieza|episode|cap[ií]tulo|season|"
                              r"temporada|continue|contin[uú]a|sigue|next|siguiente|resume)\b", t[:m.start()]):
        return False
    rest = t[m.start():]
    return any(re.search(rx, rest) for rx in SAID.values())


def has_steps(title: str) -> bool:
    """Whether a title the rules took has words of a step that can follow in it ("the bear y vete al minutes 12"):
    then the rules' reading isn't trusted for a sentence that sounds chained."""
    return any(re.search(rx, title) for key, rx in SAID.items() if key not in ("find_scene", "audio")) or bool(
        JOIN.search(title))
