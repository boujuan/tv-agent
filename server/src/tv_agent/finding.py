"""Finding an episode or a scene from what's said about it: "the one where Mr. Burns gets shot", "go to the part
where Paul rides the worm" (agent._play_found, agent._find_scene). Nothing other than what was asked plays unasked:
when it isn't clear, a list to pick from.

Episodes (episode())
  1. A name said ("Advanced Dungeons & Dragons") is matched here.
  2. Each episode's plot: Cinemeta's line, TVmaze's summary and Wikipedia's ("List of … episodes" and its season
     pages, which tell the side plots too), kept per show for a week (plots(); fetched ahead for the shows you're
     watching: prefetch_plots()).
  3. At the same time, the LLM gives the words to look for, in English and Spanish, and the episodes it knows it
     could be (look()). English requests don't wait for it.
  4. The plots are ranked here by those words (rank(): BM25, milliseconds); the best CANDIDATES, with the LLM's own
     guesses, go to the LLM with their whole plots, which names the ones that fit and whether exactly one does.
  5. One sure: its subtitles must have some of the words (within VERIFY_S; unknown counts as yes). Several that
     fit, a sure one whose subtitles say none of them, or none: up to 3 to pick from. None fitting: the subtitles
     of the likeliest episodes are searched first (corpus(), within CORPUS_S).
Scenes (scene())
  1. A description cut off while it was said is refused (chain.cut_off).
  2. The episode's subtitles in the language asked in first, hearing-impaired before plain (their [sound cues] tell
     what happens), then the other language; kept on disk, and fetched ahead when a title starts (prefetch_subs()).
     Addons that answer late (OpenSubtitles PRO: 6-9 s) aren't waited for: they fill the cache for next time.
  3. look() gives the words in both languages (names, and the other names the story uses for a thing); only the lines with
     them and the lines around them go to the LLM (MAX_LINES instead of thousands).
  4. No line has any: not in this episode. The episodes before and after are searched; found there, it's offered
     (a list of one: "yes" or "number 1" goes there), never played unasked.
  5. The LLM quotes the line it places the moment by; chain.place_scene finds its time, and it must be among the
     lines that have the words (BEFORE cues before one, WINDOW after, at most). The line shows on the TV in the language asked in.
"""
import asyncio
import gzip
import json
import logging
import math
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

import httpx

from . import chain, tracing
from .config import DATA_DIR
from .decide import text as T
from .languages import matches
from .stremio import cinemeta, subtitles

log = logging.getLogger(__name__)
CACHE = DATA_DIR / "finding"         # plots per show, subtitles per episode (cues, gzipped JSON)
PLOTS_TTL = 7 * 86400
PLOTS_S = 3.0                        # plots fetched while asked: what came by then
LOOK_S = 4.0                         # the words to look for (LLM)
LOOK_EXTRA_S = 1.2                   # an English request waits this much more for them once the text is in
SUBS_S = 1.2                         # subtitle addons that answer later fill the cache for next time
DOWNLOAD_S = 4.0
SLOW = re.compile(r"aiostreams", re.I)    # its subtitle links took 6 s and more to read (2026-10-24)
VERIFY_S = 1.5
CORPUS_S = 3.0
AT_ONCE = 8
CANDIDATES = 10
WINDOW = 6                           # lines after each line with the words
BEFORE = 10                          # and before it: a moment often comes just before the lines that react to it
GAP = 8                              # a gap this short between two passages is sent too (the same scene)
MAX_LINES = 200
AGREE_S = 4.0                        # the two placements of a moment waited for together
AGREE_MS = 30_000                    # two placements this close are the same moment
UA = "tv-agent (a personal TV voice agent; https://github.com/boujuan/tv-agent)"
WIKI = "https://en.wikipedia.org/w/api.php"
TVMAZE = "https://api.tvmaze.com"

STOP = set("""the a an and or of to in on at for with from by is are was were be been it its this that these those he
she they his her their him them who what where when which while as into out up down over about after before then
than there here just very so not no yes do does did have has had will would can could should one ones episode
episodes season scene part moment bit time times get gets got goes going make makes made el la los las un una unos
unas y o de del al en con por para que se su sus le les lo es son fue era como cuando donde mientras pero mas muy ya
no si capitulo episodio temporada escena parte momento trozo sale hay esta este""".split())
SUFFIXES = ("iendo", "ando", "ings", "ing", "ed", "es", "s", "e", "a", "o")


def fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", (text or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip()


def stem(word: str) -> str:
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def keys(text: str) -> list[str]:
    """The words of a text that say something, folded and cut to their stem ("dances", "dancing": "danc")."""
    return [stem(w) for w in fold(text).split() if len(w) >= 3 and w not in STOP]


def rank(docs: list[str], terms: list[str]) -> list[float]:
    """Each document's BM25 score for the words (a whole phrase found adds to it)."""
    want = {k for t in terms for k in keys(t)}
    if not want or not docs:
        return [0.0] * len(docs)
    toks = [keys(d) for d in docs]
    folded = [f" {fold(d)} " for d in docs]
    phrases = [f" {fold(t)} " for t in terms if len(fold(t).split()) > 1]
    n, avg = len(docs), (sum(map(len, toks)) / len(docs)) or 1.0
    df = Counter(k for t in toks for k in set(t) & want)
    out = []
    for t, f in zip(toks, folded):
        counts = Counter(t)
        score = 0.0
        for k in want:
            if c := counts.get(k):
                idf = math.log(1 + (n - df[k] + 0.5) / (df[k] + 0.5))
                score += idf * c * 2.2 / (c + 1.2 * (0.25 + 0.75 * len(t) / avg))
        score += sum(1.5 for p in phrases if p in f)
        out.append(score)
    return out


# ---- what to look for (the LLM) ----

LOOK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["en", "es", "episodes"],
               "properties": {"en": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                              "es": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                              "episodes": {"type": "array", "maxItems": 3, "items": {
                                  "type": "object", "additionalProperties": False, "required": ["season", "episode"],
                                  "properties": {"season": {"type": "integer"}, "episode": {"type": "integer"}}}}}}
LOOK_SYSTEM = """Someone asked their TV, by voice, for {what} of "{title}": "{wanted}" (maybe misremembered, maybe
in Spanish). Give the words most likely written {where}: the characters' names as the show says them, key nouns and
verbs, the other names the story uses for the same thing (a nickname, a title, what the characters call a creature
or a place). Up to 8 in English ("en") and up to 8 in Spanish ("es"), single words or short phrases. "episodes": up
to 3 episodes it could be, from what you know of the show, the likeliest first, as season and episode numbers; empty
when you don't know or it isn't about an episode. Answer only with the JSON object."""


@dataclass
class Look:
    en: list[str] = field(default_factory=list)
    es: list[str] = field(default_factory=list)
    episodes: list[tuple[int, int]] = field(default_factory=list)

    @property
    def terms(self) -> list[str]:
        return self.en + self.es


async def look(llm, title: str, wanted: str, episode: bool) -> Look:
    what, where = (("one episode", "in a summary of its plot") if episode else
                   ("a moment of what's playing", "in the subtitles around it, or in the [sound cues] of subtitles "
                                                  "for the deaf"))
    data = await llm._post({
        "max_tokens": 300,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "look_for", "strict": True, "schema": LOOK_SCHEMA}},
        "messages": [{"role": "system", "content": LOOK_SYSTEM.format(what=what, title=title, wanted=wanted,
                                                                      where=where)},
                     {"role": "user", "content": wanted}]}, LOOK_S)
    got = data["choices"][0]["message"]["content"]
    got = json.loads(got) if isinstance(got, str) else got
    words = lambda k: [str(w).strip()[:60] for w in got.get(k) or [] if str(w).strip()][:10]   # noqa: E731
    eps = [(int(e["season"]), int(e["episode"])) for e in got.get("episodes") or []
           if isinstance(e, dict) and isinstance(e.get("season"), int) and isinstance(e.get("episode"), int)]
    return Look(words("en"), words("es"), eps[:3])


def _looking(llm, title: str, wanted: str, episode: bool) -> asyncio.Task | None:
    if llm is None:
        return None

    async def run() -> Look | None:
        try:
            return await look(llm, title, wanted, episode)
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
            log.info("words to look for: %s", type(e).__name__)
            return None
    return asyncio.create_task(run())


async def _terms(task: asyncio.Task | None, wanted: str, wait_s: float,
                 stage: str = "episode") -> tuple[list[str], Look | None]:
    """The words to look for: the request's own, and the LLM's when it has answered (within wait_s)."""
    got, t = None, time.perf_counter()
    if task is not None:
        try:
            got = await asyncio.wait_for(asyncio.shield(task), wait_s)
        except asyncio.TimeoutError:
            log.info("words to look for: not in %.1f s, the request's own", wait_s)
    tracing.step(stage, "words", label="Words to look for", ms=tracing.since(t), method=tracing.method(
        "llm" if got else "local", "the LLM's (look_for)" if got else "the request's own"), own=keys(wanted),
        english=got.en if got else None, spanish=got.es if got else None,
        remembered=[f"S{a}E{b}" for a, b in got.episodes] if got and got.episodes else None,
        verdict="in time" if got else "not in time" if task is not None else "no LLM", waited_up_to_s=wait_s)
    return [wanted, *(got.terms if got else [])], got


# ---- kept on disk ----

def _path(*parts: str):
    return CACHE.joinpath(*(re.sub(r"[^\w.-]", "_", p) for p in parts))


def _read(path) -> dict | list | None:
    try:
        return json.loads(gzip.decompress(path.read_bytes()))
    except (OSError, ValueError, EOFError):
        return None


def _write(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(gzip.compress(json.dumps(data, ensure_ascii=False).encode(), 6))
        tmp.replace(path)
    except OSError as e:
        log.info("keeping %s: %s", path.name, type(e).__name__)


_plots: dict[str, tuple[float, dict]] = {}
_listings: dict[str, list[subtitles.Subtitle]] = {}
_partial: dict[str, list[subtitles.Subtitle]] = {}
_cues: dict[str, list[tuple[int, str]]] = {}
_busy: dict[str, asyncio.Task] = {}


def forget():
    """What's kept in memory (the bench's cold runs)."""
    _plots.clear()
    _listings.clear()
    _partial.clear()
    _cues.clear()
    _busy.clear()
    _failed.clear()


# ---- plots ----

def _clean_wiki(text: str) -> str:
    text = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    for _ in range(3):
        text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"<[^>]+>|'{2,}", "", text)
    return re.sub(r"\s+", " ", text).strip()


async def _wiki_text(http: httpx.AsyncClient, page: str) -> tuple[str, str]:
    r = await http.get(WIKI, params={"action": "parse", "page": page, "prop": "wikitext", "format": "json",
                                     "formatversion": "2", "redirects": "1"}, headers={"User-Agent": UA}, timeout=6)
    r.raise_for_status()
    p = r.json().get("parse") or {}
    return str(p.get("title") or page), str(p.get("wikitext") or "")


def _wiki_episodes(text: str, season: int | None) -> dict[tuple[int, int], str]:
    """{{Episode list}} templates: (season, number in it) -> title and summary. Without a season page, the season is
    the last "Season N" heading before it."""
    out = {}
    for m in re.finditer(r"\{\{\s*(?:#invoke:\s*)?Episode list(?:/sublist|\s*\|\s*sublist)?\s*\|(.*?)\n\s*\}\}", text,
                         re.S):
        fields = {}
        for part in re.split(r"\n\s*\|", "\n|" + m.group(1)):
            k, _, v = part.partition("=")
            if v:
                fields[k.strip().lower()] = v.strip()
        n = re.match(r"\d+", _clean_wiki(fields.get("episodenumber2", "")))
        s = season
        if s is None:
            heads = re.findall(r"\n=+[^=\n]*?(?:Season|Series)\s+(\d+)[^=\n]*=+", text[:m.start()], re.I)
            s = int(heads[-1]) if heads else None
        summary = _clean_wiki(fields.get("shortsummary") or fields.get("summary") or "")
        if n and s is not None and summary:
            out[(s, int(n.group()))] = f"{_clean_wiki(fields.get('title', ''))}: {summary}"
    return out


async def _wiki_pages(http: httpx.AsyncClient, titles: list[str]) -> dict[str, str]:
    """Which of these pages exist: asked title -> the page's own title (after redirects)."""
    out = {}
    for i in range(0, len(titles), 50):
        r = await http.get(WIKI, params={"action": "query", "titles": "|".join(titles[i:i + 50]), "redirects": "1",
                                         "format": "json", "formatversion": "2"}, headers={"User-Agent": UA}, timeout=6)
        r.raise_for_status()
        q = r.json().get("query") or {}
        moved = {x["from"]: x["to"] for x in (q.get("normalized") or []) + (q.get("redirects") or [])}
        there = {pg["title"] for pg in q.get("pages") or [] if not pg.get("missing") and not pg.get("invalid")}
        for t in titles[i:i + 50]:
            final = moved.get(moved.get(t, t), moved.get(t, t))
            if final in there:
                out[t] = final
    return out


async def _wikipedia(http: httpx.AsyncClient, name: str, seasons: list[int]) -> dict[tuple[int, int], str]:
    """English Wikipedia's episode summaries: its "List of … episodes" page and each season's own page (found by
    title, both ways shows name them: "X (season 1)", "X season 3")."""
    asked = {f"List of {name} episodes": 0}
    for n in seasons:
        asked[f"{name} (season {n})"] = asked[f"{name} season {n}"] = n
    found = await _wiki_pages(http, list(asked))
    pages: dict[str, int] = {}
    for t, final in found.items():
        if asked[t] == 0 or "#" not in final:
            pages.setdefault(final, asked[t])
    out: dict[tuple[int, int], str] = {}
    gate = asyncio.Semaphore(AT_ONCE)

    async def one(page: str, season: int):
        async with gate:
            try:
                return _wiki_episodes((await _wiki_text(http, page))[1], season or None)
            except (httpx.HTTPError, ValueError) as e:
                log.info("Wikipedia %s: %s", page, type(e).__name__)
                return {}
    for got in await asyncio.gather(*(one(p, n) for p, n in sorted(pages.items(), key=lambda x: x[1])[:60])):
        for key, text in got.items():
            out.setdefault(key, text)            # a season's own page first (sorted after the list: 0)
    log.info("Wikipedia: %d episode plots of %s (%d pages)", len(out), name, len(pages))
    return out


async def _tvmaze(http: httpx.AsyncClient, imdb: str) -> dict[tuple[int, int], str]:
    r = await http.get(f"{TVMAZE}/lookup/shows", params={"imdb": imdb}, timeout=5, follow_redirects=True)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    r = await http.get(f"{TVMAZE}/shows/{r.json()['id']}/episodes", timeout=6)
    r.raise_for_status()
    return {(e["season"], e["number"]): re.sub(r"<[^>]+>", "", e.get("summary") or "").strip()
            for e in r.json() if isinstance(e.get("season"), int) and isinstance(e.get("number"), int)}


async def plots(http: httpx.AsyncClient, imdb: str, name: str, eps: list[dict], wait_s: float = PLOTS_S) -> dict:
    """Each episode's plot, video id -> text: its name, Cinemeta's line, TVmaze's and Wikipedia's summaries. Kept a
    week; fetched in wait_s at most (what came by then: the rest is asked again next time)."""
    have = _plots.get(imdb) or (lambda d: (d["at"], d) if isinstance(d, dict) else None)(_read(_path("plots",
                                                                                                    f"{imdb}.json.gz")))
    if not have or time.time() - have[0] > PLOTS_TTL or have[1].get("partial"):
        job = _busy.get(f"plots {imdb}")
        if job is None or job.done():
            job = _busy[f"plots {imdb}"] = asyncio.create_task(
                _fetch_plots(http, imdb, name, sorted({v["season"] for v in eps})))
        try:
            have = await asyncio.wait_for(asyncio.shield(job), wait_s)
        except asyncio.TimeoutError:
            log.info("plots of %s: not all in %.1f s", name, wait_s)
        except Exception as e:
            log.info("plots of %s: %s", name, type(e).__name__)
    extra = (have[1].get("plots") or {}) if have else {}
    tracing.step("episode", "plots", label="Episode plots", method=tracing.method(
        "cache" if have and not job_started(imdb) else "web", "Cinemeta + TVmaze + Wikipedia"), episodes=len(eps),
        with_summaries=len(extra), partial=bool(have and have[1].get("partial")) or None,
        age_h=round((time.time() - have[0]) / 3600, 1) if have else None,
        verdict="kept" if have else "not in time: Cinemeta's lines only")
    return {v["id"]: " ".join(x for x in (v.get("name") or "", v.get("overview") or v.get("description") or "",
                                          extra.get(f"{v['season']}:{v.get('episode')}", "")) if x)
            for v in eps}


def job_started(imdb: str) -> bool:
    """Whether this command fetched the plots (else they were kept)."""
    return f"plots {imdb}" in _fetched


_fetched: set[str] = set()


async def _fetch_plots(http: httpx.AsyncClient, imdb: str, name: str, seasons: list[int]) -> tuple[float, dict]:
    t = time.perf_counter()
    got = await asyncio.gather(_tvmaze(http, imdb), _wikipedia(http, name, seasons), return_exceptions=True)
    _fetched.add(f"plots {imdb}")
    tracing.step("episode", "plot sources", label="Plots fetched", ms=tracing.since(t),
                 method=tracing.method("web", "TVmaze + English Wikipedia"),
                 tvmaze=len(got[0]) if isinstance(got[0], dict) else type(got[0]).__name__,
                 wikipedia=len(got[1]) if isinstance(got[1], dict) else type(got[1]).__name__)
    merged: dict[str, str] = {}
    for part in got:
        if isinstance(part, BaseException):
            log.info("plots of %s: %s", name, type(part).__name__)
            continue
        for (s, e), text in part.items():
            key = f"{s}:{e}"
            merged[key] = f"{merged[key]} {text}" if key in merged else text
    data = {"at": time.time(), "plots": merged, "partial": any(isinstance(p, BaseException) for p in got)}
    _plots[imdb] = (data["at"], data)
    _write(_path("plots", f"{imdb}.json.gz"), data)
    return data["at"], data


def prefetch_plots(http: httpx.AsyncClient, shows: list[tuple[str, str]]):
    """The plots of these shows (imdb id, name), fetched in the background when not kept or a week old."""
    async def run():
        for imdb, name in shows:
            if f"plots {imdb}" in _busy and not _busy[f"plots {imdb}"].done():
                continue
            kept = _read(_path("plots", f"{imdb}.json.gz"))
            if isinstance(kept, dict) and time.time() - kept.get("at", 0) < PLOTS_TTL and not kept.get("partial"):
                continue
            try:
                seasons = sorted({v["season"] for v in cinemeta.episodes(await cinemeta.meta(http, "series", imdb))})
                await _fetch_plots(http, imdb, name, seasons)
            except Exception as e:
                log.info("plots of %s ahead: %s", name, type(e).__name__)
    return asyncio.create_task(run())


# ---- subtitles ----

def _kind(video: str) -> str:
    return "series" if video.count(":") == 2 else "movie"


async def _listing(http, addons, video: str, wait_s: float, stream=None) -> list[subtitles.Subtitle]:
    """The subtitles the addons offer for a video (by its id, and the file playing when there's one: its release's
    first): what has answered within wait_s. One lookup per video: the slower addons go on in the background, and
    are there for the next call."""
    if video in _listings:
        return _listings[video]
    job = _busy.get(f"list {video}")
    if job is None or job.done():
        found = _partial[video] = []
        serving = [a for a in addons if a.serves("subtitles", _kind(video), video)
                   and not subtitles.SKIP.search(a.name)]

        async def one(addon):
            found.extend(await subtitles.fetch(http, [addon], _kind(video), video, stream, timeout=10))

        async def every():
            await asyncio.gather(*(one(a) for a in serving), return_exceptions=True)
            if len(_listings) > 300:
                _listings.clear()
            _listings[video] = list(found)
            _partial.pop(video, None)
        job = _busy[f"list {video}"] = asyncio.create_task(every())
    try:
        await asyncio.wait_for(asyncio.shield(job), wait_s)
    except asyncio.TimeoutError:
        pass
    return _listings.get(video) or list(_partial.get(video, []))


async def track(http, addons, video: str, lang: str, wait_s: float = SUBS_S, sdh_wanted: bool = False, stream=None,
                label: str = "") -> tuple[list[tuple[int, str]], bool]:
    """track_() into the trace when `label` says what for."""
    t = time.perf_counter()
    lines, sdh = await track_(http, addons, video, lang, wait_s, sdh_wanted, stream)
    failed = _failed.pop(f"{video}.{lang}", None)
    if label:
        src = _source.get(f"{video}.{lang}.{'sdh' if sdh else 'plain'}", failed or "none came in time")
        tracing.step("scene", "subtitles", label=label, ms=tracing.since(t),
                     method=tracing.method("cache" if src.startswith("kept") else "addon", src), video=video,
                     language=lang, hearing_impaired=sdh, lines=len(lines),
                     verdict="found" if lines else "failed" if failed else "none", why=None if lines else failed)
    return lines, sdh


_source: dict[str, str] = {}             # where each set of subtitles came from, for the trace
_failed: dict[str, str] = {}             # "<video>.<lang>": subtitles offered whose downloads all failed, and why


async def track_(http, addons, video: str, lang: str, wait_s: float = SUBS_S,
                 sdh_wanted: bool = False, stream=None) -> tuple[list[tuple[int, str]], bool]:
    """A video's subtitles in a language, as (start ms, words) lines, and whether they're for the hearing-impaired
    (those first: their [sound cues] tell what happens). Kept on disk; [] when none came in time. `sdh_wanted`:
    plain ones kept aren't enough while the addons may offer hearing-impaired ones (fetching ahead)."""
    plain = None
    for sdh in (True, False):
        key = f"{video}.{lang}.{'sdh' if sdh else 'plain'}"
        if key not in _cues and isinstance(kept := _read(_path("subs", f"{key}.json.gz")), list):
            _cues[key] = [(int(ms), str(w)) for ms, w in kept]
            _source.setdefault(key, "kept on disk")
        if _cues.get(key):
            if sdh:
                return _cues[key], True
            plain = _cues[key]
    if plain and not sdh_wanted:
        return plain, False
    offered = [s for s in await _listing(http, addons, video, wait_s, stream)
               if matches({"language": s.language, "name": s.name}, lang)]
    if plain and not any(s.impaired for s in offered):
        return plain, False
    best = sorted(offered, key=lambda s: (not s.impaired, s.ai, SLOW.search(s.addon) is not None))[:2]
    failed = []

    async def get(sub) -> list[tuple[int, str]]:
        try:
            return subtitles.cues(await subtitles.download(http, sub.url, timeout=DOWNLOAD_S))
        except (httpx.HTTPError, OSError, ValueError) as e:
            log.info("subtitles of %s from %s: %s", video, sub.addon, type(e).__name__)
            code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
            failed.append(f"{sub.addon}: {f'HTTP {code}' if code else type(e).__name__}")
            return []
    for sub, got in zip(best, await asyncio.gather(*(get(sub) for sub in best))):   # both at once, the first kept
        if len(got) >= 20:
            key = f"{video}.{lang}.{'sdh' if sub.impaired else 'plain'}"
            if len(_cues) > 300:
                _cues.clear()
            _cues[key] = got
            _source[key] = f"{sub.addon}: {sub.name[:80]}"
            _write(_path("subs", f"{key}.json.gz"), got)
            return got, sub.impaired
    if failed and not plain:
        if len(_failed) > 300:
            _failed.clear()
        _failed[f"{video}.{lang}"] = "offered, but the downloads failed (" + ", ".join(failed) + ")"
    return plain or [], False


def prefetch_subs(http, addons_fn, video: str, stream=None):
    """A title that just started: its subtitles in English and Spanish, hearing-impaired ones too, from every addon
    (the slow ones included), so a scene asked for later is found at once."""
    async def run():
        try:
            addons = await addons_fn()
            if not (job := _busy.get(f"list {video}")) or job.done():
                _listings.pop(video, None)             # every addon asked again, the slow ones waited for
            await _listing(http, addons, video, 30, stream)
            await asyncio.gather(*(track(http, addons, video, lang, 30, sdh_wanted=True, stream=stream)
                                   for lang in ("eng", "spa")))
        except Exception as e:
            log.info("subtitles ahead for %s: %s", video, type(e).__name__)
    return asyncio.create_task(run())


# ---- episodes ----

PICK_SCHEMA = lambda ids: {"type": "object", "additionalProperties": False, "required": ["fits"],  # noqa: E731
                           "properties": {"fits": {"type": "array", "maxItems": 3, "items": {
                               "type": "object", "additionalProperties": False, "required": ["id", "match"],
                               "properties": {"id": {"type": "string", "enum": ids},
                                              "match": {"type": "string", "enum": ["exactly", "partly"]}}}}}}
PICK_SYSTEM = """Someone asked their TV for one episode of a series by what happens in it, or its name ("wanted",
maybe misremembered, maybe in Spanish). Below are the episodes it may be, with their plots; what you know about the
series counts too. Answer only with the JSON object: "fits", the episodes that fit, best first (at most 3; empty when
none does), each with "match": "exactly" when what was asked happens in it (the event, the line, the place asked
about), "partly" when it only shares some of it (the same characters, a related story, the next part). A story told
over several episodes: the part where what was asked happens is "exactly", the others "partly". A holiday or a theme
the show did more than once (several Halloween episodes): each one that has it is "exactly"."""


@dataclass
class EpisodeFind:
    episode: dict | None = None          # the one: play it
    choices: list[dict] = field(default_factory=list)    # else these to pick from, likeliest first
    why: str = ""                        # how it was found, or why not


async def episode(agent, imdb: str, name: str, eps: list[dict], wanted: str) -> EpisodeFind:
    tracing.step("episode", "asked", label="Episode asked for", imdb=imdb, title=name, wanted=wanted)
    found = await _episode(agent, imdb, name, eps, wanted)
    tracing.step("episode", "verdict", label="Episode", method=tracing.method("local", "finding.py"),
                 verdict="play" if found.episode else "list" if found.choices else "none", why=found.why,
                 episode=found.episode["id"] if found.episode else None,
                 choices=[v["id"] for v in found.choices] or None)
    return found


async def _episode(agent, imdb: str, name: str, eps: list[dict], wanted: str) -> EpisodeFind:
    t0 = time.perf_counter()
    want = fold(wanted.replace("&", " and "))
    named = sorted(((_ratio(want, fold((v.get("name") or "").replace("&", " and "))), i) for i, v in enumerate(eps)),
                   reverse=True)
    if named and named[0][0] >= 90 and (len(named) == 1 or named[1][0] < named[0][0]):
        tracing.step("episode", "name", label="Matched by its name", ms=tracing.since(t0),
                     method=tracing.method("local", "fuzzy name match"), confidence=named[0][0], threshold=90,
                     episode=eps[named[0][1]]["id"], verdict="accepted")
        return EpisodeFind(eps[named[0][1]], why="its name")
    if agent.llm is None:
        return EpisodeFind(why="finding an episode by what happens in it needs the LLM")
    looking = _looking(agent.llm, name, wanted, True)
    texts = await plots(agent.http, imdb, name, eps)
    t_plots = time.perf_counter()
    # Spanish needs the words in English (the plots are); English waits less for them (and the LLM's guesses):
    # without them the candidates, and so the answer, changed from one try to the next
    terms, got = await _terms(looking, wanted, LOOK_S if T.spanish(wanted) else LOOK_EXTRA_S)
    t_look = time.perf_counter()
    scores = rank([texts[v["id"]] for v in eps], terms)
    order = sorted(range(len(eps)), key=lambda i: -scores[i])
    picked = [eps[i] for i in order[:CANDIDATES]]
    by_number = {(v["season"], v.get("episode")): v for v in eps}
    guessed = []
    for s, e in (got.episodes if got else []):
        if (v := by_number.get((s, e))) and v not in picked:
            picked.append(v)
            guessed.append(v["id"])
    tracing.step("episode", "ranking", label="Plots ranked", ms=tracing.since(t_look),
                 method=tracing.method("local", "BM25 over the plots"), episodes=len(eps),
                 top=[{"id": eps[i]["id"], "name": eps[i].get("name"), "score": round(scores[i], 2)}
                      for i in order[:CANDIDATES]], added_from_memory=guessed or None,
                 detail={"plots": {v["id"]: texts.get(v["id"], "") for v in picked}})
    addons = await agent.addons()
    ahead = {v["id"]: asyncio.create_task(_lines(agent.http, addons, v["id"], VERIFY_S)) for v in picked[:3]}
    if order and scores[order[0]] > 0:                # its streams meanwhile: the plots' best is usually the answer
        agent.prefetch("series", picked[0]["id"])
    try:
        fits, sure, marks = await _pick(agent.llm, name, wanted, picked, texts)
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
        log.info("episode choice unavailable (%s)", type(e).__name__)
        tracing.step("episode", "choice", label="Which fit", verdict="failed", why=type(e).__name__)
        return EpisodeFind(choices=picked[:3], why="couldn't ask which")
    tracing.step("episode", "choice", label="Which fit", method=tracing.method("llm", agent.llm.model),
                 fits=marks or None, verdict="one exactly" if sure else "several" if fits else "none",
                 why=None if sure else "sure only when exactly one fits exactly")
    log.info("episode %r of %s: %s%s (%d candidates; plots %.0f ms, words %.0f ms more, choice %.0f ms more)", wanted,
             name, [v["id"] for v in fits], " sure" if sure else "", len(picked), (t_plots - t0) * 1000,
             (t_look - t_plots) * 1000, (time.perf_counter() - t_look) * 1000)
    if fits and sure:
        agent.prefetch("series", fits[0]["id"])     # while its subtitles are checked (one lookup per video at a time)
        if got is None and looking is not None and looking.done():
            got = looking.result()
        job = ahead.get(fits[0]["id"]) or asyncio.create_task(_lines(agent.http, addons, fits[0]["id"], VERIFY_S))
        t = time.perf_counter()
        words = [wanted, *(got.terms if got else [])]
        said = await _said_in(job, words, 0.4 if fits[0]["id"] in ahead else VERIFY_S)
        have = set(keys(" ".join(w for _, w in job.result()))) if job.done() and not job.exception() else set()
        tracing.step("episode", "subtitle check", label="Checked in its subtitles", ms=tracing.since(t),
                     method=tracing.method("addon", "its subtitles"), episode=fits[0]["id"],
                     found=sorted({k for w in words for k in keys(w)} & have) or None,
                     verdict={True: "said in it", False: "none of the words", None: "no subtitles in time"}[said])
        if said is False:
            return EpisodeFind(choices=fits[:3] or picked[:3], why="its subtitles say nothing of it")
        return EpisodeFind(fits[0], why="its plot" + (" and subtitles" if said else ""))
    if fits:
        return EpisodeFind(choices=fits[:3], why="more than one fits")
    terms, got = await _terms(looking, wanted, LOOK_S) if got is None else (terms, got)
    found = await corpus(agent, [v for v in [eps[i] for i in order] if v not in fits][:30], terms)
    return EpisodeFind(choices=found or picked[:3], why="found in the subtitles" if found else "none fits")


def _ratio(a: str, b: str) -> float:
    from rapidfuzz import fuzz
    return fuzz.token_set_ratio(a, b) if a and b else 0.0


async def _pick(llm, series: str, wanted: str, candidates: list[dict], texts: dict) -> tuple[list[dict], bool]:
    ids = [v["id"] for v in candidates]
    data = await llm._post({
        "max_tokens": 120,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "episodes", "strict": True, "schema": PICK_SCHEMA(ids)}},
        "messages": [{"role": "system", "content": PICK_SYSTEM},
                     {"role": "user", "content": json.dumps({"series": series, "wanted": wanted, "episodes": [
                         {"id": v["id"], "about": f"S{v['season']}E{v.get('episode')} {texts.get(v['id'], '')[:900]}"}
                         for v in candidates]}, ensure_ascii=False)}]}, 8.0)
    got = data["choices"][0]["message"]["content"]
    got = json.loads(got) if isinstance(got, str) else got
    fits = [f for f in got.get("fits") or [] if isinstance(f, dict) and f.get("id") in ids]
    exact = [f for f in fits if f.get("match") == "exactly"]
    # sure: exactly one episode has what was asked (the others only share some of it)
    return ([next(v for v in candidates if v["id"] == f["id"]) for f in fits], len(exact) == 1 and fits[0] in exact,
            [{"id": f["id"], "match": f.get("match")} for f in fits])


async def _lines(http, addons, video: str, wait_s: float) -> list[tuple[int, str]]:
    """An episode's subtitles, English else Spanish (what's kept, or what the fast addons give in wait_s)."""
    for lang in ("eng", "spa"):
        if (got := (await track(http, addons, video, lang, wait_s))[0]):
            return got
    return []


async def _said_in(job: asyncio.Task | None, terms: list[str], wait_s: float) -> bool | None:
    """Whether an episode's subtitles (being fetched since the candidates were known) have any of the words; None
    when they aren't in by wait_s."""
    if job is None:
        return None
    try:
        lines = await asyncio.wait_for(asyncio.shield(job), wait_s)
    except (asyncio.TimeoutError, Exception) as e:
        log.info("an episode's subtitles to check it by: %s", type(e).__name__)
        return None
    if not lines:
        return None
    have = set(keys(" ".join(w for _, w in lines)))
    return any(k in have for t in terms for k in keys(t))


async def corpus(agent, eps: list[dict], terms: list[str]) -> list[dict]:
    """The episodes whose subtitles say the most of the words, searched in the plots' order within CORPUS_S (what's
    kept first, then AT_ONCE at a time; the rest is kept for next time): up to 3 that say at least two of them."""
    want = {k for t in terms for k in keys(t)}
    if not want:
        return []
    addons = await agent.addons()
    gate = asyncio.Semaphore(AT_ONCE)
    found: dict[str, int] = {}

    async def one(v):
        async with gate:
            lines, _ = await track(agent.http, addons, v["id"], "eng", 1.0)
            if lines:
                found[v["id"]] = len(want & set(keys(" ".join(w for _, w in lines))))
    jobs = [asyncio.create_task(one(v)) for v in eps]
    t = time.perf_counter()
    if jobs:
        await asyncio.wait(jobs, timeout=CORPUS_S)
    best = sorted((n, i) for i, n in found.items() if n >= 2)[::-1][:3]
    log.info("subtitles of %d of %d episodes searched: %s", len(found), len(eps), best)
    tracing.step("episode", "subtitle search", label="Searched their subtitles", ms=tracing.since(t),
                 method=tracing.method("addon", "subtitles of the likeliest episodes"), searched=len(found),
                 of=len(eps), within_s=CORPUS_S, words=sorted(want), best=[{"id": i, "words": n} for n, i in best]
                 or None, verdict="found" if best else "none says two of the words")
    return [v for _, i in best for v in eps if v["id"] == i]


# ---- scenes ----

PLACE_SYSTEM = """Someone watching a film or episode ("watching") asked to jump to a moment they described
("wanted", maybe misremembered). Below are lines of its subtitles that may be near it, one cue per line: "m:ss text"
(or "h:mm:ss"); "…" between parts far apart; [in brackets]: sounds, in subtitles for the deaf. Find where that
moment starts: what's said just before or as it happens; a moment without words is between the lines around it.
The line you place it by is said before the moment or as it happens, never after it: a line that explains, names or
reacts to what just happened (what it was, how it was done, someone asking about it) comes after, so the moment is
earlier: place it by the last line before the action. What you know about the story helps tell which of these parts it is (the first time it happens, unless they say
otherwise). Answer only with the JSON object: "line", the subtitle you placed it by (said in the moment, or the last
line before it), copied exactly without its time; "at", the time listed before that line, copied exactly ("51:25"); "how":
"words" when that line is said in the moment itself or right as it happens, "around" when the moment has no words
of its own and the lines around it place it, "guess" when these lines don't show it; and "shown": that line in
{language}, as the TV will show it (the line itself when it already is). All null when it isn't there."""
PLACE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["line", "at", "how", "shown"],
                "properties": {"line": {"type": ["string", "null"]}, "at": {"type": ["string", "null"]},
                               "how": {"type": ["string", "null"], "enum": ["words", "around", "guess", None]},
                               "shown": {"type": ["string", "null"]}}}


@dataclass
class SceneFind:
    ms: int | None = None                # where the moment starts (the line it was placed by)
    line: str = ""
    shown: str = ""                      # that line as the TV shows it, in the language asked in
    video: str = ""                      # the episode it's in, when it isn't the one playing (an offer)
    why: str = ""
    lang: str = ""                       # the subtitles it was found in
    lines_sent: int = 0
    cues: list = field(default_factory=list, repr=False)    # those subtitles (their times may need a shift)
    others: list = field(default_factory=list)   # other places it was put in: not sure, pick one


def clock(ms: int) -> str:
    return chain.clock(ms)


def windows(cues: list[tuple[int, str]], terms: list[str]) -> tuple[list[int], list[int], float]:
    """The lines with the words, the indexes of the passages around them that go to the LLM (BEFORE lines before
    each, WINDOW after, a short GAP between two filled, in order, MAX_LINES at most), and the best passage's score. A passage counts the different words it has, each by
    how rare it is in the episode: a name, a thing and its other name together beat one sound cue alone (subtitles
    for the deaf can repeat a sound all through a film)."""
    want = {k for t in terms for k in keys(t)}
    if not want or not cues:
        return [], [], 0.0
    own = set(keys(terms[0])) if terms else set()        # the request's own words count more than the LLM's
    has = [set(keys(w)) & want for _, w in cues]
    df = Counter(k for h in has for k in h)
    idf = {k: math.log(1 + len(cues) / (1 + n)) * (1.5 if k in own else 1.0) for k, n in df.items()}
    hits = [i for i, h in enumerate(has) if h]
    scored = []
    for i in hits:
        around = range(max(0, i - BEFORE), min(len(cues), i + WINDOW + 1))
        scored.append((sum(idf[k] for k in set().union(*(has[j] for j in around))), i))
    keep: set[int] = set()
    for _, i in sorted(scored, reverse=True):
        around = set(range(max(0, i - BEFORE), min(len(cues), i + WINDOW + 1)))
        if len(keep | around) > MAX_LINES:
            break
        keep |= around
    kept = sorted(keep)
    for a, b in zip(kept, kept[1:]):
        if 1 < b - a <= GAP + 1 and len(keep) + b - a - 1 <= MAX_LINES:
            keep.update(range(a + 1, b))
    return hits, sorted(keep), max((x for x, _ in scored), default=0.0)


async def scene(agent, video: str, title: str, wanted: str, heard: str = "", stream=None) -> SceneFind:
    """Where a described moment of `video` is (or of an episode next to it); see the module's docstring."""
    t0 = time.perf_counter()
    if agent.llm is None:
        return SceneFind(why="Finding a moment needs the LLM (Settings › API keys: OpenRouter)")
    spanish = T.spanish(wanted) or T.spanish(heard)
    langs = ["spa", "eng"] if spanish else ["eng", "spa"]
    tracing.step("scene", "asked", label="Moment asked for", video=video, title=title, wanted=wanted,
                 language="Spanish" if spanish else "English")
    looking = _looking(agent.llm, title, wanted, False)
    addons = await agent.addons()
    first = asyncio.create_task(track(agent.http, addons, video, langs[0], stream=stream,
                                      label=f"{'Spanish' if langs[0] == 'spa' else 'English'} subtitles (asked in it)"))
    second = asyncio.create_task(track(agent.http, addons, video, langs[1], stream=stream,
                                       label=f"{'Spanish' if langs[1] == 'spa' else 'English'} subtitles"))
    terms, got = await _terms(looking, wanted, LOOK_S if spanish else LOOK_EXTRA_S, "scene")
    t_look = time.perf_counter()
    for lang, job in zip(langs, (first, second)):
        cues, sdh = await job
        log.info("scene: %s subtitles of %s in %.0f ms (%d lines)", lang, video, (time.perf_counter() - t0) * 1000,
                 len(cues))
        hits, keep, best = windows(cues, terms)
        _passages(lang, sdh, cues, hits, keep, terms, best)
        if hits:
            found = await _place(agent.llm, title, wanted, cues, hits, keep, "Spanish" if spanish else "English")
            found.lang, found.cues = f"{lang}{' (hearing-impaired)' if sdh else ''}", cues
            log.info("moment %r of %s: %s%s in %s subtitles, %d of %d lines sent (words %.0f ms, then %.0f ms)%s",
                     wanted, title, clock(found.ms) if found.ms is not None else found.why,
                     f" (also {[clock(f.ms) if f.ms is not None else f.why for f in found.others]})"
                     if found.others else "", found.lang, found.lines_sent, len(cues), (t_look - t0) * 1000,
                     (time.perf_counter() - t_look) * 1000, f"; line {found.line!r}" if found.ms is None else "")
            if found.ms is not None or found.why != "not in these lines":
                return found
    if not (first.result()[0] or second.result()[0]):
        return SceneFind(why="No subtitles to search in this one")
    near = await _neighbours(agent, video, title, wanted, terms, langs, "Spanish" if spanish else "English")
    return near or SceneFind(why="it isn't in the subtitles (a moment without words can't be found this way)")


def _passages(lang: str, sdh: bool, cues, hits: list[int], keep: list[int], terms: list[str], best: float):
    """The passages chosen, into the trace (their lines in the record's details)."""
    if not cues:
        return
    want = {k for t in terms for k in keys(t)}
    matched = Counter(k for i in hits for k in set(keys(cues[i][1])) & want)
    lines, last = [], None
    for i in keep:
        if last is not None and i != last + 1:
            lines.append("…")
        lines.append(f"{clock(cues[i][0])} {cues[i][1]}")
        last = i
    tracing.step("scene", "passages", label="Passages sent", method=tracing.method(
        "local", "passages holding the most rare words"), language=lang, hearing_impaired=sdh, lines=len(cues),
        sent=len(keep), lines_with_words=len(hits), words=dict(matched.most_common(12)) or None,
        best_score=round(best, 2), verdict="sent" if hits else "no line has the words",
        detail={"passages": "\n".join(lines)} if keep else None)


async def _place(llm, title: str, wanted: str, cues, hits: list[int], keep: list[int], language: str) -> SceneFind:
    """Where in these passages the moment is: asked twice at once (no longer than once). Both answers at the same
    place: there. Two places: both, to pick from (`others`); the same model has placed one moment at 51:25 one time
    and 32:23 the next. One answer within AGREE_S: that one."""
    lines, last = [], None
    for i in keep:
        if last is not None and i != last + 1:
            lines.append("…")
        lines.append(f"{clock(cues[i][0])} {cues[i][1][:120]}")
        last = i
    body = {"max_tokens": 220,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "moment", "strict": True, "schema": PLACE_SCHEMA}},
            "messages": [{"role": "system", "content": PLACE_SYSTEM.format(language=language)},
                         {"role": "user", "content": json.dumps({"watching": title, "wanted": wanted},
                                                                ensure_ascii=False) + "\n\nSubtitles:\n" +
                          "\n".join(lines)}]}
    asks = [asyncio.create_task(_one_place(llm, body, cues, hits, keep)) for _ in range(2)]
    done, pending = await asyncio.wait(asks, timeout=AGREE_S)
    if not done:                                  # neither yet: the first that answers
        done, pending = await asyncio.wait(asks, timeout=12.0, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    got = [t.result() for t in asks if t in done and not t.cancelled() and t.exception() is None]
    found = await _settle(llm, body, cues, hits, keep, got)
    tracing.step("scene", "agreement", label="Answers compared", method=tracing.method(
        "local", f"two at once, a third when they differ (same moment: within {AGREE_MS // 1000} s)"),
        answers=[clock(f.ms) if f.ms is not None else f.why for f in got], verdict=(
            "not placed" if found.ms is None else "listed to pick from" if found.others else "agreed"),
        placed=clock(found.ms) if found.ms is not None else None)
    return found


async def _settle(llm, body: dict, cues, hits: list[int], keep: list[int], got: list[SceneFind]) -> SceneFind:
    placed = [f for f in got if f.ms is not None]
    if not placed:
        return got[0] if got else SceneFind(why="Couldn't search for that moment right now", lines_sent=len(keep))
    if len(got) == 2 and (len(placed) == 1 or abs(placed[0].ms - placed[1].ms) > AGREE_MS):
        third = await _one_place(llm, body, cues, hits, keep)         # they differ: a third answer settles it
        got.append(third)
        for f in placed:
            if third.ms is not None and abs(third.ms - f.ms) <= AGREE_MS:
                return min(f, third, key=lambda x: x.ms)       # the same moment: where it starts
        placed = [f for f in got if f.ms is not None]
    elif len(placed) == 2:                        # the same moment: where it starts
        return min(placed, key=lambda x: x.ms)
    first = placed[0]
    first.others = [f for f in placed[1:] if abs(f.ms - first.ms) > AGREE_MS]
    if len(placed) < len(got) and not first.others:     # placed once, and not the other times: not sure either
        first.others.append(SceneFind(why="not placed"))
    return first


async def _one_place(llm, body: dict, cues, hits: list[int], keep: list[int]) -> SceneFind:
    found, got = await _one_place_(llm, body, cues, hits, keep)
    tracing.step("scene", "answer", label="An answer", method=tracing.method("llm", llm.model),
                 line=found.line or (got or {}).get("line") or None, listed_at=(got or {}).get("at"),
                 how=(got or {}).get("how"), placed=clock(found.ms) if found.ms is not None else None,
                 verdict="placed" if found.ms is not None else "refused", why=found.why or None)
    return found


async def _one_place_(llm, body: dict, cues, hits: list[int], keep: list[int]) -> tuple[SceneFind, dict | None]:
    t = time.perf_counter()
    try:
        data = await llm._post(dict(body), 12.0)
        log.info("scene: placed in %.0f ms", (time.perf_counter() - t) * 1000)
        got = data["choices"][0]["message"]["content"]
        got = json.loads(got) if isinstance(got, str) else got
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
        log.info("scene search unavailable (%s)", type(e).__name__)
        return SceneFind(why="Couldn't search for that moment right now", lines_sent=len(keep)), None
    line = re.sub(r"^\d+:\d\d(?::\d\d)?\s+", "", str(got.get("line") or ""))[:160]
    if not line and not got.get("at"):
        return SceneFind(why="not in these lines", lines_sent=len(keep)), got
    sent = [cues[i] for i in keep]
    ms, why = chain.place_scene(str(got.get("at") or ""), line, str(got.get("how") or ""), sent)
    if ms is None:
        return SceneFind(why=why, line=line, lines_sent=len(keep)), got
    at = next(i for i in keep if cues[i][0] == ms)
    if not any(h - BEFORE <= at <= h + WINDOW for h in hits):
        return SceneFind(why="nothing of what you said is near the line it found", line=line,
                         lines_sent=len(keep)), got
    shown = re.sub(r"^\d+:\d\d(?::\d\d)?\s+", "", str(got.get("shown") or ""))[:160] or line
    return SceneFind(ms, line, shown, lines_sent=len(keep)), got


async def _neighbours(agent, video: str, title: str, wanted: str, terms, langs, language: str) -> SceneFind | None:
    """Not in this episode: the ones before and after it, searched the same way; where it is, as an offer."""
    m = re.match(r"^(tt\d+):(\d+):(\d+)$", video)
    if not m:
        return None
    try:
        eps = cinemeta.episodes(await cinemeta.meta(agent.http, "series", m.group(1)))
    except (httpx.HTTPError, ValueError, AssertionError) as e:
        log.info("episodes next to %s: %s", video, type(e).__name__)
        return None
    ids = [v["id"] for v in eps]
    if video not in ids:
        return None
    i = ids.index(video)
    addons = await agent.addons()
    near = [ids[j] for j in (i - 1, i + 1) if 0 <= j < len(ids)]

    async def lines_of(v: str) -> list[tuple[int, str]]:
        for lang in langs:
            if (cues := (await track(agent.http, addons, v, lang, 2.0))[0]):
                return cues
        return []
    best, t = None, time.perf_counter()
    scores = {}
    for v, cues in zip(near, await asyncio.gather(*(lines_of(v) for v in near))):
        hits, keep, score = windows(cues, terms)
        scores[v] = round(score, 2) if hits else None
        if hits and (best is None or score > best[0]):
            best = (score, v, cues, hits, keep)
    tracing.step("scene", "neighbours", label="The episodes before and after", ms=tracing.since(t),
                 method=tracing.method("local", "the same search in their subtitles"), scores=scores or None,
                 verdict=f"in {best[1]}" if best else "not there either")
    if best is None:
        return None
    found = await _place(agent.llm, title, wanted, best[2], best[3], best[4], language)
    if found.ms is None:
        return None
    found.video, found.cues = best[1], best[2]
    log.info("moment %r: not in %s but in %s at %s", wanted, video, best[1], clock(found.ms))
    return found
