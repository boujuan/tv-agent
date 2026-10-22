"""Reading titles, seasons, episodes and amounts out of a sentence. Used by the rule converters (rules.py) and by
the d1 path (router.py)."""
import re

from .numbers import season_episode, to_int

PLAY_VERBS = re.compile(
    r"^(?:i(?:'d| would) like to (?:watch|see)|i (?:want|wanna) to (?:watch|see)|i wanna (?:watch|see)|i want|let'?s watch|can we watch|"
    r"go back to|go to|jump to|skip to|switch to|move to|take me to|"
    r"play|put on|put|watch|start|show me|show|stream|open|find|search for|look for)(?:\s+(?:me\s+)?|$)")
RESTART = re.compile(r"\b(?:from the (?:start|beginning)|from scratch|from episode one|start over|restart)\b")
KIND_MOVIE = re.compile(r"^(?:the )?(?:movie|film)\s+|\s+(?:the )?(?:movie|film)$")
KIND_SERIES = re.compile(r"^(?:the )?(?:series|show|tv show)\s+|\s+(?:the )?(?:series|show|tv show)$")
EDGE_WORDS = re.compile(r"^(?:of|from|for|some|on|in)\s+|\s+(?:on|in|for|please|now|again)$")
# "Go to season 3 episode 4", "carry on with the show": these mean the series that's on, not a title.
CURRENT = {"", "it", "this", "that", "this one", "the show", "this show", "the series", "this series", "the episode",
           "this episode", "the current show", "the current episode", "my show", "the last episode", "last episode",
           "the same show", "the same series"}
WHERE_I_WAS = {"watching", "what i was watching", "where i left off", "where we left off", "the last thing",
               "what we were watching", "where i was"}


SPANISH = re.compile(r"[ñáéíóú¿¡]|\b(?:qu[eé]|puedes|hacer|sabes|dime|ayuda|ayudame|comandos|pon|ponme|el|la|los|las|"
                     r"del|en|un|una|y|es|est[aá]|por|para|c[oó]mo|d[oó]nde|cu[aá]l|tienes|algo|esto)\b")


def spanish(text: str) -> bool:
    """Whether a sentence is Spanish (for what's shown back, like the "what can you do?" list)."""
    return bool(SPANISH.search(text.lower()))


def clean_title(text: str) -> str:
    t = PLAY_VERBS.sub("", re.sub(r"\s+", " ", text.strip()))
    t = RESTART.sub("", t)
    t = re.sub(r"^the (?=(?:of|from) )", "", t)   # "the [first episode] of the last of us"
    for _ in range(3):
        t = EDGE_WORDS.sub("", t.strip()).strip(" ,.")
    return t


def title_slots(text: str) -> dict:
    """title, season, episode, kind, restart from a 'play ...' sentence."""
    restart = bool(RESTART.search(text))
    season, episode, rest = season_episode(text)
    kind = None
    rest = clean_title(rest)
    if KIND_MOVIE.search(rest):
        kind, rest = "movie", KIND_MOVIE.sub("", rest)
    elif KIND_SERIES.search(rest):
        kind, rest = "series", KIND_SERIES.sub("", rest)
    if season is not None or episode is not None:
        kind = "series"
    slots = {"title": rest.strip() or None, "season": season, "episode": episode, "kind": kind}
    if restart:
        slots["restart"] = True
    return slots


def episode_here(text: str) -> dict | None:
    """'go to season 3 episode 4', 'episode five', 'season 2': an episode of the series that's on (no title)."""
    season, episode, rest = season_episode(text)
    if season is None and episode is None:
        return None
    rest = re.sub(r"^(?:of|in|from) ", "", clean_title(rest)).strip()
    if rest not in CURRENT:
        return None
    return {"title": None, "season": season, "episode": episode, "kind": "series",
            "restart": bool(RESTART.search(text))}


def continue_title(rest: str) -> str | None:
    """What follows 'continue …': None for the show that's on or 'where I left off', else the title."""
    rest = re.sub(r"^(?:playing|watching|with)\s*", "", rest.strip()).strip()
    if rest in CURRENT or rest in WHERE_I_WAS:
        return None
    return title_slots(rest)["title"] or rest


def amount(text: str | None) -> int | None:
    """'a bit' 3, 'a lot' 15, or a number ('by five')."""
    t = (text or "").strip()
    if not t:
        return None
    if re.search(r"\ba lot\b|\bmuch\b", t):
        return 15
    if re.search(r"\ba (?:little )?bit\b|\ba little\b|\bslightly\b", t):
        return 3
    return to_int(t.replace("by ", "").strip())


def query(text: str) -> str:
    """A YouTube search as said, without a leading 'some', 'a' or 'videos of'."""
    q = text.strip()
    return re.sub(r"^(?:some|a|the video|videos? of)\s+", "", q) if len(q.split()) > 1 else q
