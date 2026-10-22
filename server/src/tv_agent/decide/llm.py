"""The last step for what the rules and d1 leave undecided: a general LLM through OpenRouter (`llm_model`, by default
Gemini 2.5 Flash-Lite, ~0.7-1 s), asked for one action in a fixed JSON shape. Only with an OpenRouter key
(`tv-agent setup openrouter`) and `llm_fallback` on. Research: docs/research-2026-10-22.md §2.5.

It gets what you said, what's playing, the app names and up to 10 of your library's titles that sound like what you
said, so a misheard name can still be matched ("the bare" -> The Bear). Requests ask for providers that keep no data
(zero data retention) and don't train on it. Its answer is checked like everything else: a title has to be in what
you said or among those candidates, numbers have to be in range, and the result is one of the agent's own actions.
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
ACTIONS = ["play_title", "continue_watching", "next_episode", "previous_episode", "pause", "resume", "stop", "forward",
           "back", "seek_to", "volume_up", "volume_down", "volume_set", "mute", "unmute", "open_app", "youtube", "home",
           "subtitles", "audio", "skip_intro", "skip_recap", "skip_credits", "show_recent", "show_new", "pick", "none"]
SLOTS = {"title": "string", "season": "integer", "episode": "integer", "seconds": "integer", "number": "integer",
         "app": "string", "language": "string", "query": "string"}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action", *SLOTS],
          "properties": {"action": {"type": "string", "enum": ACTIONS},
                         **{k: {"type": [t, "null"]} for k, t in SLOTS.items()}}}
SYSTEM = """You turn one spoken command for a TV voice agent into one action. The words come from speech-to-text,
so some may be misheard. Answer only with the JSON object. Use "none" when it isn't a TV command or you aren't sure.

Actions and their fields (every other field null):
play_title: title, season, episode (a film or series by name) · continue_watching: title, or null for the last one
next_episode, previous_episode, pause, resume, stop, home, mute, unmute, volume_up, volume_down
forward / back: seconds (skip ahead / back) · seek_to: seconds from the start · volume_set: number 0-100
open_app: app (one of "apps") · youtube: query · subtitles: language, "on" or "off" · audio: language or "original"
skip_intro, skip_recap, skip_credits · show_recent ("what was I watching") · show_new ("what's new")
pick: number (an item of the list on screen)

Titles: copy one exactly from "candidates" when it's what was said, even misheard ("the bare" is "The Bear");
otherwise copy the title as said. Never invent a title. Numbers as digits ("season three" -> 3, "a minute" -> 60).
Languages in English ("spanish"). The command may be in Spanish.

Examples:
"put that bear show on, third season" -> {"action":"play_title","title":"The Bear","season":3}
"can you make it so I can actually hear them" -> {"action":"volume_up"}
"I missed what she said, go back a bit" -> {"action":"back","seconds":10}
"what was the weather like yesterday" -> {"action":"none"}
"bring up that list of things I was in the middle of" -> {"action":"show_recent"}
"the second thing on the list" -> {"action":"pick","number":2}"""


class Llm:
    def __init__(self, http: httpx.AsyncClient, key: str, model: str, rules: Rules):
        self.http, self.key, self.model, self.rules = http, key, model, rules

    async def ask(self, said: str, context: dict, timeout: float = 4.0) -> dict | None:
        body = {"model": self.model, "temperature": 0, "max_tokens": 200,
                "provider": {"require_parameters": True, "zdr": True, "data_collection": "deny", "sort": "latency"},
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "tv_action", "strict": True, "schema": SCHEMA}},
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": json.dumps({"said": said, **context}, ensure_ascii=False)}]}
        r = await self.http.post(URL, json=body, timeout=timeout,
                                 headers={"Authorization": f"Bearer {self.key}", "X-Title": "tv-agent"})
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(content) if isinstance(content, str) else content

    async def decide(self, said: str, context: dict) -> A.Action | None:
        try:
            answer = await self.ask(said, context)
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
            log.info("LLM unavailable (%s)", type(e).__name__)
            return None
        return to_action(answer or {}, said, context.get("candidates", []), self.rules)


def to_action(a: dict, said: str, candidates: list[str], rules: Rules) -> A.Action | None:
    """The LLM's answer as one of the agent's actions, or None when it doesn't hold up."""
    kind = a.get("action")
    num = lambda k: a.get(k) if isinstance(a.get(k), int) else None          # noqa: E731
    title = (a.get("title") or "").strip() or None
    if title and not (any(fuzz.ratio(title.lower(), c.lower()) >= 90 for c in candidates)
                      or fuzz.partial_ratio(title.lower(), said.lower()) >= 70):
        log.info("LLM title %r is neither said nor a candidate: ignored", title)
        return None
    lang = (a.get("language") or "").strip().lower()
    code = LANGUAGE_NAMES.get(lang)
    try:
        match kind:
            case "play_title" if title:
                return A.PlayTitle(title=title, season=num("season"), episode=num("episode"))
            case "continue_watching":
                return A.ContinueWatching(title=title)
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
