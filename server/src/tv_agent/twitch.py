"""Spoken streamer name -> Twitch login. Speech-to-text spells names freely ("eye bye" for ibai), so the
favourites in config.toml (twitch_favorites) are matched first; otherwise the words are joined as a login."""
import re

from rapidfuzz import fuzz, process

LOGIN = re.compile(r"^[A-Za-z0-9_]{3,25}$")


def resolve(spoken: str, favorites: list[str]) -> str | None:
    if favorites:
        hit = process.extractOne(spoken.lower(), [f.lower() for f in favorites], scorer=fuzz.WRatio, score_cutoff=75)
        if hit:
            return favorites[hit[2]]
    login = re.sub(r"[^a-z0-9_]", "", spoken.lower())
    return login if LOGIN.match(login) else None
