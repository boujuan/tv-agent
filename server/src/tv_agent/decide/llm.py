"""The last step for what the rules and d1 leave undecided: a general LLM through OpenRouter (`llm_model`, by default
Gemini 3.5 Flash-Lite), asked for one action in a fixed JSON shape. Only with an OpenRouter key
(`tv-agent setup openrouter`) and `llm_fallback` on. Research: docs/research-2026-10-22.md §2.5. Gemini 3.5
Flash-Lite always reasons a little; `llm_reasoning` keeps it at "minimal" (its floor; "" sends nothing, for models
that take no reasoning setting).

It gets what you said, what's playing, the app names and up to 10 of your library's titles that sound like what you
said, so a misheard name can still be matched ("the bare" -> The Bear), and a title can come from its own knowledge
("the one with the robbers in Dalí masks" -> La casa de papel). The fastest provider that enforces the JSON shape
answers (`llm_private` keeps to providers that keep no data, which is slower for Gemini 3.5 Flash-Lite). Its answer
is checked like everything else: numbers in range, an app from the list, one of the agent's own actions; a title it
names still has to be found by the title search before anything plays.
"""
import json
import logging

import httpx
from pydantic import ValidationError
from rapidfuzz import fuzz

from .. import actions as A
from .rules import LANGUAGE_NAMES, Rules

log = logging.getLogger(__name__)
URL = "https://openrouter.ai/api/v1/chat/completions"
FAST = {"require_parameters": True, "sort": "latency"}       # only endpoints that enforce the JSON shape
# Zero data retention only. Gemini 3.5 Flash-Lite's one such endpoint (Vertex) doesn't list every parameter, so they
# can't be required there: the schema is still sent, and the answer is checked here anyway.
PRIVATE = {"zdr": True, "data_collection": "deny", "sort": "latency"}
ACTIONS = ["play_title", "continue_watching", "show_in_stremio", "next_episode", "previous_episode", "pause", "resume",
           "stop", "home", "start_over", "replay", "forward", "back", "seek_to", "seek_percent", "volume_up",
           "volume_down", "volume_set", "mute", "unmute", "open_app", "youtube", "twitch", "subtitles", "audio",
           "skip_intro", "skip_recap", "skip_credits", "stop_after_this", "turn_off_after_this", "stop_in", "turn_off_in",
           "cancel_timer", "binge_on", "binge_off", "library_add", "library_remove", "mark_watched", "mark_unwatched",
           "show_recent", "show_new", "show_movies", "pick", "help", "none"]
SLOTS = {"title": "string", "season": "integer", "episode": "integer", "seconds": "integer", "number": "integer",
         "app": "string", "language": "string", "query": "string"}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action", *SLOTS],
          "properties": {"action": {"type": "string", "enum": ACTIONS},
                         **{k: {"type": [t, "null"]} for k, t in SLOTS.items()}}}
SYSTEM = """You turn one spoken command for a TV voice agent into one action. The words come from speech-to-text,
so some may be misheard. Answer only with the JSON object. Use "none" when it isn't a TV command or you aren't sure.

Actions and their fields (every other field null):
play_title: title, season, episode · continue_watching: title, or null for what was watched last
show_in_stremio: title (its page, without playing)
next_episode, previous_episode, pause, resume, stop, home, start_over, replay ("what did they say?")
forward / back: seconds · seek_to: seconds from the start · seek_percent: number (50 = the middle)
volume_up, volume_down, mute, unmute · volume_set: number 0-100
open_app: app (one of "apps") · youtube: query · twitch: query (the streamer)
subtitles: language, "on" or "off" · audio: language or "original"
skip_intro, skip_recap, skip_credits
stop_after_this, turn_off_after_this (when what's playing ends) · stop_in, turn_off_in: number (minutes)
cancel_timer · binge_on, binge_off (the next episode starts by itself)
library_add, library_remove, mark_watched, mark_unwatched: title, season, episode, or all null for what's on
show_recent ("what was I watching"), show_new ("what's new"), show_movies (new films), help ("what can you do")
pick: number (an item of the list on screen)

Titles: copy one exactly from "candidates" when it's what was said, even misheard ("the bare" is "The Bear"). When
a film or series is described instead of named, give its real title. Otherwise copy the title as said.
Numbers as digits ("season three" -> 3, "a couple of minutes" -> 120 seconds). Languages in English ("spanish").
The command may be in Spanish.

Examples:
"put that bear show on, third season" -> {"action":"play_title","title":"The Bear","season":3}
"the series with the chemistry teacher who cooks meth" -> {"action":"play_title","title":"Breaking Bad"}
"can you make it so I can actually hear them" -> {"action":"volume_up"}
"I'm off to bed, switch it all off when this one's done" -> {"action":"turn_off_after_this"}
"take me to the halfway point" -> {"action":"seek_percent","number":50}
"is ibai live?" -> {"action":"twitch","query":"ibai"}
"este ya lo he visto" -> {"action":"mark_watched"}
"empieza el capítulo otra vez" -> {"action":"start_over"}
"go back to what I was watching yesterday" -> {"action":"continue_watching"}
"the second thing on the list" -> {"action":"pick","number":2}
"what was the weather like yesterday" -> {"action":"none"}"""


class Llm:
    def __init__(self, http: httpx.AsyncClient, key: str, model: str, rules: Rules, reasoning: str = "minimal",
                 private: bool = False):
        self.http, self.key, self.model, self.rules, self.reasoning = http, key, model, rules, reasoning
        self.private = private

    async def ask(self, said: str, context: dict, timeout: float = 3.0) -> dict | None:
        body = {"model": self.model, "temperature": 0, "max_tokens": 400,
                "provider": PRIVATE if self.private else FAST,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "tv_action", "strict": True, "schema": SCHEMA}},
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": json.dumps({"said": said, **context}, ensure_ascii=False)}]}
        if self.reasoning:
            body["reasoning"] = {"effort": self.reasoning, "exclude": True}
        r = await self.http.post(URL, json=body, timeout=timeout,
                                 headers={"Authorization": f"Bearer {self.key}", "X-Title": "tv-agent"})
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        answer = json.loads(content) if isinstance(content, str) else content
        return answer if isinstance(answer, dict) else None

    async def answer(self, said: str, context: dict) -> tuple[bool, A.Action | None]:
        """(whether it answered, its action or None for "not a command")."""
        try:
            raw = await self.ask(said, context)
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
            log.info("LLM unavailable (%s)", type(e).__name__)
            return False, None
        return True, to_action(raw or {}, said, context.get("candidates", []), self.rules)

    async def decide(self, said: str, context: dict) -> A.Action | None:
        return (await self.answer(said, context))[1]


def to_action(a: dict, said: str, candidates: list[str], rules: Rules) -> A.Action | None:
    """The LLM's answer as one of the agent's actions, or None when it doesn't hold up."""
    kind = a.get("action")
    num = lambda k: a.get(k) if isinstance(a.get(k), int) else None          # noqa: E731
    title = (a.get("title") or "").strip() or None
    if title and not (any(fuzz.ratio(title.lower(), c.lower()) >= 90 for c in candidates)
                      or fuzz.partial_ratio(title.lower(), said.lower()) >= 70):
        log.info("LLM title %r comes from its own knowledge (not said, not in your library)", title)
    lang = (a.get("language") or "").strip().lower()
    code = LANGUAGE_NAMES.get(lang)
    library = {"library_add": "add", "library_remove": "remove", "mark_watched": "watched", "mark_unwatched": "unwatched"}
    try:
        match kind:
            case "play_title" if title:
                return A.PlayTitle(title=title, season=num("season"), episode=num("episode"))
            case "continue_watching":
                return A.ContinueWatching(title=title)
            case "show_in_stremio" if title:
                return A.OpenInStremio(title=title, season=num("season"), episode=num("episode"))
            case "start_over":
                return A.Transport(op="seek_to", seconds=0)
            case "replay":
                return A.Replay(seconds=num("seconds") or 10)
            case "seek_percent" if num("number") is not None:
                return A.Transport(op="seek_to", percent=num("number"))
            case "twitch" if a.get("query"):
                return A.Twitch(channel=a["query"])
            case "stop_after_this" | "turn_off_after_this":
                return A.Timer(then="off" if kind.startswith("turn_off") else "stop")
            case "stop_in" | "turn_off_in" if num("number"):
                return A.Timer(minutes=num("number"), then="off" if kind.startswith("turn_off") else "stop")
            case "cancel_timer":
                return A.Timer(cancel=True)
            case "binge_on" | "binge_off":
                return A.Binge(on=kind == "binge_on")
            case "library_add" | "library_remove" | "mark_watched" | "mark_unwatched":
                return A.LibraryEdit(op=library[kind], title=title, season=num("season"), episode=num("episode"))
            case "show_movies":
                return A.ShowList(list="movies")
            case "help":
                return A.Help()
            case "next_episode" | "previous_episode":
                return A.NextEpisode(offset=1 if kind == "next_episode" else -1)
            case "pause" | "resume" | "stop":
                return A.Transport(op=kind)
            case "forward" | "back":
                return A.Transport(op=kind, seconds=num("seconds"))
            case "seek_to" if num("seconds") is not None:
                return A.Transport(op="seek_to", seconds=num("seconds"))
            case "volume_up" | "volume_down":
                return A.Volume(op=kind.removeprefix("volume_"))
            case "volume_set" if num("number") is not None:
                return A.Volume(op="set", amount=num("number"))
            case "mute" | "unmute":
                return A.Volume(op=kind)
            case "open_app" if (app := rules.find_app((a.get("app") or "").lower())):
                return A.OpenApp(app=app.key)
            case "youtube" if a.get("query"):
                return A.YouTube(query=a["query"])
            case "home":
                return A.Home()
            case "subtitles" if lang in ("on", "off") or code:
                return A.Tracks(subtitles=lang if lang in ("on", "off") else code)
            case "audio" if lang == "original" or code:
                return A.Tracks(audio="original" if lang == "original" else code)
            case "skip_intro" | "skip_recap" | "skip_credits":
                return A.Skip(what=kind.removeprefix("skip_"))
            case "show_recent" | "show_new":
                return A.ShowList(list=kind.removeprefix("show_"))
            case "pick" if num("number"):
                return A.Pick(number=num("number"))
    except ValidationError:
        log.info("LLM answer out of range: %s", kind)
    return None
