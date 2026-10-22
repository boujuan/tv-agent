"""Spoken numbers and durations: "three", "twenty one", "third", "1 hour 5 minutes", "a minute and a half"."""
import re

UNITS = {"zero": 0, "oh": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
        "ninety": 90}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
            "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
WORD = "|".join(sorted([*UNITS, *TENS, "hundred"], key=len, reverse=True))
NUMBER = rf"(?:\d+(?:\.\d+)?|(?:(?:{WORD})(?:[ -](?:{WORD}))*))"
UNIT_S = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600}


def to_int(text: str) -> int | None:
    """'21', 'twenty one', 'twenty-one', 'a hundred', 'third' -> int; None if it isn't a number."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+", text):
        return int(text)
    if text in ORDINALS:
        return ORDINALS[text]
    total, seen = 0, False
    for w in re.split(r"[ -]+", text):
        if w in UNITS:
            total += UNITS[w]
        elif w in TENS:
            total += TENS[w]
        elif w == "hundred":
            total = max(total, 1) * 100
        elif w == "and":
            continue
        else:
            return None
        seen = True
    return total if seen else None


DURATION = re.compile(rf"\b({NUMBER}|half an?|a half)\s*(hours?|minutes?|mins?|seconds?|secs?)\b(?:\s+and\s+a\s+half)?")


def seconds_in(text: str) -> int | None:
    """Total of every duration in the text: 'one hour five minutes' -> 3900, 'a minute and a half' -> 90."""
    total, found = 0, False
    for m in DURATION.finditer(text.lower()):
        amount_text, unit = m.group(1), m.group(2).rstrip("s")
        unit = "min" if unit.startswith("min") else ("sec" if unit.startswith("sec") else unit)
        if amount_text.startswith(("half", "a half")):
            amount = 0.5
        else:
            n = float(amount_text) if re.fullmatch(r"\d+(?:\.\d+)?", amount_text) else to_int(amount_text)
            if n is None:
                continue
            amount = n
        if m.group(0).endswith("and a half"):
            amount += 0.5
        total += amount * UNIT_S[unit]
        found = True
    return int(total) if found else None


ORD = "|".join(ORDINALS)
SEASON_EP = [   # (regex, True when the episode comes first)
    (re.compile(rf"\bs(?:eason)?\s*({NUMBER})\s*,?\s*(?:e|ep|episode)\s*({NUMBER})\b"), False),
    (re.compile(rf"\b(?:episode|ep)\s+({NUMBER})\s*,?\s+(?:of\s+|from\s+|in\s+)?(?:the\s+)?"
                rf"(?:season|series)\s+({NUMBER}|{ORD})\b"), True),
    (re.compile(rf"\b(?:the\s+)?({ORD})\s+episode\s+(?:of|from|in)\s+(?:the\s+)?"
                rf"(?:season|series)\s+({NUMBER}|{ORD})\b"), True),
    (re.compile(r"\bs(\d{1,2})\s*e(\d{1,3})\b"), False),
]
SEASON = re.compile(rf"\b(?:season|series)\s+({NUMBER}|{ORD})\b|\b(?:the\s+)?({ORD})\s+season\b")
EPISODE = re.compile(rf"\b(?:episode|ep)\s+({NUMBER})\b|\b(?:the\s+)?({ORD})\s+episode\b")


def _cut(t: str, m: re.Match) -> str:
    return re.sub(r"\s+", " ", t[:m.start()] + " " + t[m.end():]).strip()


def season_episode(text: str) -> tuple[int | None, int | None, str]:
    """(season, episode, text with them removed) from 'season three episode two', 's3e2', 'episode 2 of season 3',
    'the tenth episode of season five'."""
    t = text.lower()
    for rx, episode_first in SEASON_EP:
        m = rx.search(t)
        if m:
            a, b = to_int(m.group(1)), to_int(m.group(2))
            season, episode = (b, a) if episode_first else (a, b)
            return season, episode, _cut(t, m)
    season = episode = None
    if m := SEASON.search(t):
        season = to_int(m.group(1) or m.group(2))
        t = _cut(t, m)
    if m := EPISODE.search(t):
        episode = to_int(m.group(1) or m.group(2))
        t = _cut(t, m)
    return season, episode, t
