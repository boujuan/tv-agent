"""A general LLM through OpenRouter (`llm_model`), asked for one action in a fixed JSON shape: one of the decision
steps (router.py; its place and on/off in the panel's Decisions tab). Only with an OpenRouter key
(`tv-agent setup openrouter`). Research: docs/research-2026-10-22.md §2.5 and §2.9. `llm_reasoning` sets the
reasoning effort for models that reason ("" sends nothing, for models that take no reasoning setting).

It gets what you said, what's playing, the app names and up to 10 of your library's titles that sound like what you
said, so a misheard name can still be matched, and a title can come from its own knowledge (a film or series
described instead of named). The fastest provider that enforces the JSON shape
answers (`llm_private` keeps to providers that keep no data). Its answer is checked like everything else: numbers in
range, an app from the list, one of the agent's own actions; a title it names still has to be found by the title
search before anything plays. `titles()` asks it which real titles a name the search can't find sounds like; the
agent keeps the one that sounds closest. `recommend()` suggests real titles for a request ("something like X", "a
short comedy"), with your recent viewing as taste; the agent checks each one with
the title search before it lists it.
"""
import json
import logging
import time

import httpx
from pydantic import ValidationError
from rapidfuzz import fuzz

from .. import actions as A
from .. import tracing
from .rules import LANGUAGE_NAMES, Rules

log = logging.getLogger(__name__)
URL = "https://openrouter.ai/api/v1/chat/completions"
FAST = {"require_parameters": True, "sort": "latency"}       # only endpoints that enforce the JSON shape
# Zero data retention only. Gemini 3.5 Flash-Lite's one such endpoint (Vertex) doesn't list every parameter, so they
# can't be required there: the schema is still sent, and the answer is checked here anyway.
PRIVATE = {"zdr": True, "data_collection": "deny", "sort": "latency"}
ACTIONS = ["play_title", "continue_watching", "show_in_stremio", "info", "next_episode", "previous_episode", "pause",
           "resume", "stop", "home", "start_over", "replay", "forward", "back", "seek_to", "seek_percent", "volume_up",
           "volume_down", "volume_set", "mute", "unmute", "open_app", "youtube", "twitch", "subtitles_on",
           "subtitles_off", "subtitles_toggle", "subtitles", "audio", "default_subtitles", "default_audio",
           "skip_intro", "skip_recap", "skip_credits",
           "auto_skip_on", "auto_skip_off", "stop_after_this", "turn_off_after_this", "stop_in", "turn_off_in",
           "cancel_timer", "binge_on", "binge_off", "library_add", "library_remove", "mark_watched", "mark_unwatched",
           "show_recent", "show_new", "show_movies", "recommend", "find_scene", "pick", "help", "wrong", "cancel",
           "none"]
SLOTS = {"title": "string", "season": "integer", "episode": "integer", "episode_name": "string", "seconds": "integer",
         "number": "integer", "app": "string", "language": "string", "query": "string"}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["action", *SLOTS],
          "properties": {"action": {"type": "string", "enum": ACTIONS},
                         **{k: {"type": [t, "null"]} for k, t in SLOTS.items()}}}
SYSTEM = """You turn one spoken command for a TV voice agent into one action. Answer only with the JSON object.
Use "none" when it isn't a TV command or you can't tell which one it is.

The words come from speech-to-text, often of a Spanish speaker, so command words get misheard: pick the command it
sounds like ("of" or "all" after subtitles, "oh" after mode: on / off; "beach mode", "pinch mode": binge mode;
"sops", "sobs": subs). The command may be in Spanish.

Actions and their fields (every other field null):
play_title: title, season, episode, episode_name (an episode named or described instead of numbered: its name as
said, or what happens in it, in English; season and episode only when their numbers are said) · continue_watching:
title, or null for what was watched last
show_in_stremio: title (its page, without playing)
info: title, or null for what's playing ("what am I watching", "what episode of X am I on", "where am I in X")
next_episode, previous_episode, pause, resume, stop, home, start_over, replay ("what did they say?")
forward / back: seconds · seek_to: seconds from the start · seek_percent: number (50 = the middle)
volume_up, volume_down, mute, unmute · volume_set: number 0-100
open_app: app (one of "apps") · youtube: query · twitch: query (the streamer)
subtitles_on, subtitles_off · subtitles_toggle (on or off unclear) · subtitles: language · audio: language or
"original"
default_audio: language or "original" · default_subtitles: language, "on", "off" or "foreign" (only when the audio
isn't theirs): from now on, as their settings ("a partir de ahora, audio en español", "always English subtitles")
skip_intro (the opening, its credits or theme), skip_recap, skip_credits (the end credits) · auto_skip_on,
auto_skip_off (intros skipped by themselves)
stop_after_this, turn_off_after_this (when what's playing ends) · stop_in, turn_off_in: number (minutes)
cancel_timer · binge_on, binge_off (the next episode starts by itself)
library_add, library_remove, mark_watched, mark_unwatched: title, season, episode, or all null for what's on
show_recent (a list of what was being watched, not finished yet), show_new ("what's new"), show_movies (new films),
help ("what can you do") · recommend: query (they want suggestions: "something like X", "a short comedy", "what
should we watch?"; the query is their request, in their words)
find_scene: query (a moment of what's playing described in words, in English: "go to the part where he gets
shot"). Going to an episode ("ve al episodio de … donde", "¿puedes ir al episodio en el que…?") is play_title.
pick: number (an item of the list on screen) · wrong ("that's wrong": the last command was misunderstood)
cancel ("cancel", "never mind": stop what was asked a moment ago, still on its way; not playback itself)

Titles: copy one exactly from "candidates" when it's what was said, even misheard (a word heard as another that
sounds alike, words split or run together). When a film or series is described instead of named, give its real
title. Otherwise copy the title as said. In the examples, X and Y stand for any title, name or event.
Numbers as digits ("season three" -> 3, "a couple of minutes" -> 120 seconds). Languages in English ("spanish").

Examples:
"put that X show on, third season" -> {"action":"play_title","title":"X","season":3}
"pon X, el capítulo del Y" -> {"action":"play_title","title":"X","episode_name":"Y"}
"the X episode where Y gets shot" -> {"action":"play_title","title":"X","episode_name":"Y gets shot"}
"can you make it so I can actually hear them" -> {"action":"volume_up"}
"I'm off to bed, switch it all off when this one's done" -> {"action":"turn_off_after_this"}
"take me to the halfway point" -> {"action":"seek_percent","number":50}
"beach mode oh" -> {"action":"binge_on"}
"sops off" -> {"action":"subtitles_off"}
"de ahora en adelante quiero todo sin subtítulos" -> {"action":"default_subtitles","language":"off"}
"is X live on twitch?" -> {"action":"twitch","query":"X"}
"what episode of X am I on" -> {"action":"info","title":"X"}
"este ya lo he visto" -> {"action":"mark_watched"}
"empieza el capítulo otra vez" -> {"action":"start_over"}
"go back to what I was watching yesterday" -> {"action":"continue_watching"}
"what can I say?" -> {"action":"help"}
"the second thing on the list" -> {"action":"pick","number":2}
"algo de miedo que no sea muy largo" -> {"action":"recommend","query":"algo de miedo que no sea muy largo"}
"vete al minuto en el que le disparan" -> {"action":"find_scene","query":"he gets shot"}
"¿puedes ir al episodio de X en el que Y se casa?" -> {"action":"play_title","title":"X","episode_name":"Y gets married"}
"what was the weather like yesterday" -> {"action":"none"}"""


# What each call is for (by its answer's schema name), as the trace labels it
PURPOSE = {"tv_action": "Decide the command", "tv_steps": "Read a chain", "title": "Sound-alike titles",
           "suggestions": "Suggestions", "look_for": "Words to look for", "episodes": "Which episode fits",
           "moment": "Where the moment is"}


TITLE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["titles"],
                "properties": {"titles": {"type": "array", "items": {"type": "string"}, "maxItems": 3}}}
TITLE_SYSTEM = """Someone asked their TV for a film or series by voice. Speech-to-text wrote its name down wrong, as
"spelled", and a title search found nothing by that name. Say it out loud: which real films or series sound like
that? Give up to 3 of their real titles (English or original name, as on IMDb), the closest-sounding first. Names
are misheard the way speech-to-text mishears: a sound taken for a near one (b/v, th/d/t, an ending dropped), words
split or run together, a title in English written the way a Spanish speaker says it. "watched" lists titles they
have watched; use one only when it sounds like "spelled". Answer only with the JSON object; an empty list when nothing
sounds like it."""


REC_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["max_minutes", "titles"],
              "properties": {"max_minutes": {"type": ["integer", "null"]},
                             "titles": {"type": "array", "maxItems": 10, "items": {
                  "type": "object", "additionalProperties": False, "required": ["title", "year", "kind", "why"],
                  "properties": {"title": {"type": "string"}, "year": {"type": ["integer", "null"]},
                                 "kind": {"type": "string", "enum": ["movie", "series"]},
                                 "why": {"type": "string"}}}}}}
REC_SYSTEM = """Someone asked their TV, by voice, for something to watch ("request", from speech-to-text, maybe in
Spanish). Suggest 10 real films or series that fit it, the best fit first. When they name a title ("something like
X"), suggest similar ones, not that one. When they describe what they want ("a short comedy", "algo de miedo de los
80"), every suggestion must meet every condition they give: genre, length, decade, country, language, mood, film or
series ("kind", when given). "taste" lists what they watched lately, newest first: lean towards it when the request
leaves room. Leave out what's in "seen". Give each title as on IMDb (original or English name), its year of release,
movie or series, and "why": one short reason, at most 8 words, in "language". "max_minutes": the longest a film may
run when they set a limit ("under 90 minutes": 90, "menos de hora y media": 90, "something short": 100), else null.
Answer only with the JSON object."""


class Llm:
    def __init__(self, http: httpx.AsyncClient, key: str, model: str, rules: Rules, reasoning: str = "minimal",
                 private: bool = False):
        self.http, self.key, self.model, self.rules, self.reasoning = http, key, model, rules, reasoning
        self.private = private
        self.last_usage: dict = {}
        self._params: dict[str, list[tuple[bool, bool]]] = {}   # per model: (temperature?, reasoning?) that worked
        self._floor: dict[str, str] = {}             # models that can't turn reasoning off: their lowest

    async def _post(self, body: dict, timeout: float) -> dict:
        """One request. Temperature 0 and the reasoning setting where the model takes them: OpenRouter finds no
        provider for a parameter a model doesn't take (404), so it's asked again without the temperature, then
        without the reasoning setting, then without both; a model that can't turn reasoning off is asked at its
        lowest. What worked is remembered."""
        t0 = time.perf_counter()
        body = {"model": self.model, "max_tokens": 400, "provider": PRIVATE if self.private else FAST, **body}
        effort = self._floor.get(self.model, self.reasoning) if self.reasoning == "none" else self.reasoning
        tries = self._params.get(self.model) or [(True, True), (False, True), (True, False), (False, False)]
        for temperature, reasoning in list(tries):
            body.pop("temperature", None)
            body.pop("reasoning", None)
            if temperature:
                body["temperature"] = 0
            if reasoning and effort:
                body["reasoning"] = {"effort": effort, "exclude": True}
            r = await self.http.post(URL, json=body, timeout=timeout,
                                     headers={"Authorization": f"Bearer {self.key}", "X-Title": "tv-agent"})
            if r.status_code == 400 and effort == "none" and "mandatory" in r.text.lower():
                self._floor[self.model] = effort = "minimal"
                body["reasoning"] = {"effort": effort, "exclude": True}
                r = await self.http.post(URL, json=body, timeout=timeout,
                                         headers={"Authorization": f"Bearer {self.key}", "X-Title": "tv-agent"})
            if r.status_code == 404 and "parameters" in r.text and len(tries) > 1:
                continue
            r.raise_for_status()
            self._params[self.model] = [(temperature, reasoning)]
            data = r.json()
            self._traced(body, data, t0, reasoning and effort)
            return data
        raise httpx.HTTPError("no provider takes this model's parameters")

    def _traced(self, body: dict, data: dict, t0: float, effort):
        """The call into the command's trace: what it was for, the model, time, tokens and cost; its prompt and
        answer in the record's details."""
        name = ((body.get("response_format") or {}).get("json_schema") or {}).get("name", "")
        usage = data.get("usage") or {}
        try:
            answer = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            answer = None
        tracing.step("llm", name or "call", label=PURPOSE.get(name, name or "LLM call"), ms=tracing.since(t0),
                     method=tracing.method("llm", self.model), tokens_in=usage.get("prompt_tokens"),
                     tokens_out=usage.get("completion_tokens"), cost=usage.get("cost"), reasoning=effort or None,
                     detail={"messages": body.get("messages"), "answer": answer})

    async def ask(self, said: str, context: dict, timeout: float = 3.0) -> dict | None:
        data = await self._post({
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "tv_action", "strict": True, "schema": SCHEMA}},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps({"said": said, **context}, ensure_ascii=False)}]},
            timeout)
        self.last_usage = data.get("usage") or {}       # tokens (for the benchmark's cost)
        content = data["choices"][0]["message"]["content"]
        answer = json.loads(content) if isinstance(content, str) else content
        return answer if isinstance(answer, dict) else None

    async def titles(self, heard: str, sentence: str, library: list[str]) -> list[str]:
        """Real titles a misheard one may be (a name misheard -> the real titles it sounds like), most likely first."""
        try:
            data = await self._post({
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "title", "strict": True, "schema": TITLE_SCHEMA}},
                "messages": [{"role": "system", "content": TITLE_SYSTEM},
                             {"role": "user", "content": json.dumps({"spelled": heard, "sentence": sentence,
                                                                     "watched": library}, ensure_ascii=False)}]},
                3.0)
            content = data["choices"][0]["message"]["content"]
            got = (json.loads(content) if isinstance(content, str) else content).get("titles")
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, AttributeError) as e:
            log.info("LLM title fix unavailable (%s)", type(e).__name__)
            return []
        return [t.strip()[:120] for t in got if isinstance(t, str) and t.strip()][:3] if isinstance(got, list) else []

    async def recommend(self, request: str, kind: str | None, taste: list[str], seen: list[str],
                        language: str) -> tuple[list[dict], int | None]:
        """Up to 10 suggestions for a request, {title, year, kind, why}, best first (the agent checks each title),
        and the longest a film may run when the request set a limit."""
        data = await self._post({
            "max_tokens": 900,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "suggestions", "strict": True, "schema": REC_SCHEMA}},
            "messages": [{"role": "system", "content": REC_SYSTEM},
                         {"role": "user", "content": json.dumps({"request": request, "kind": kind, "taste": taste,
                                                                 "seen": seen, "language": language},
                                                                ensure_ascii=False)}]},
            12.0)
        self.last_usage = data.get("usage") or {}
        content = data["choices"][0]["message"]["content"]
        answer = json.loads(content) if isinstance(content, str) else content
        got, limit = answer.get("titles"), answer.get("max_minutes")
        out = []
        for t in got if isinstance(got, list) else []:
            if isinstance(t, dict) and isinstance(t.get("title"), str) and t["title"].strip() \
                    and t.get("kind") in ("movie", "series"):
                year = t.get("year") if isinstance(t.get("year"), int) else None
                out.append({"title": t["title"].strip()[:120], "year": year, "kind": t["kind"],
                            "why": str(t.get("why") or "").strip()[:80]})
        return out[:10], limit if isinstance(limit, int) and 20 <= limit <= 400 else None

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
    # the language is sometimes put in another text field ("query": "on")
    lang = (a.get("language") or a.get("query") or a.get("app") or "").strip().lower()
    code = LANGUAGE_NAMES.get(lang)
    library = {"library_add": "add", "library_remove": "remove", "mark_watched": "watched", "mark_unwatched": "unwatched"}
    try:
        match kind:
            case "play_title" if title:
                return A.PlayTitle(title=title, season=num("season"), episode=num("episode"),
                                   episode_name=(a.get("episode_name") or "").strip()[:200] or None)
            case "continue_watching":
                return A.ContinueWatching(title=title)
            case "show_in_stremio" if title:
                return A.OpenInStremio(title=title, season=num("season"), episode=num("episode"))
            case "info":
                return A.Info(title=title)
            case "subtitles_on" | "subtitles_off" | "subtitles_toggle":
                return A.Tracks(subtitles=kind.removeprefix("subtitles_"))
            case "auto_skip_on" | "auto_skip_off":
                return A.AutoSkip(on=kind == "auto_skip_on")
            case "wrong":
                return A.Wrong()
            case "cancel":
                return A.Cancel()
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
            case "default_subtitles" if lang in ("on", "off", "foreign") or code:
                return A.Tracks(subtitles=lang if lang in ("on", "off", "foreign") else code, keep=True)
            case "default_audio" if lang == "original" or code:
                return A.Tracks(audio="original" if lang == "original" else code, keep=True)
            case "skip_intro" | "skip_recap" | "skip_credits":
                return A.Skip(what=kind.removeprefix("skip_"))
            case "show_recent" | "show_new":
                return A.ShowList(list=kind.removeprefix("show_"))
            case "pick" if num("number"):
                return A.Pick(number=num("number"))
            case "recommend":
                return A.Recommend(query=((a.get("query") or "").strip() or said)[:300])
            case "find_scene":
                return A.FindScene(what=((a.get("query") or "").strip() or said)[:300])
    except ValidationError:
        log.info("LLM answer out of range: %s", kind)
    return None
