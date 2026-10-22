"""Intro, recap and credits timestamps for an episode, from TheIntroDB (crowd-sourced; timestamps grouped by the
length of the file they were made on) and IntroDB (one timeline per episode; Infuse and AIOStreams use it). Both are
asked with the episode's IMDb id, season and episode, and TheIntroDB also with the file's length. Research and the
rules: docs/research-2026-10-22.md §2.6.

- Skipping when asked: the segment has to contain where you are (you're in the intro when you say "skip intro"), so a
  timestamp made on another cut of the episode can't send you somewhere wrong.
- Skipping by itself (auto_skip): TheIntroDB's timestamps made on a file within 2 s of this one's length, from 3 or
  more people at confidence 0.5 or more with no competing timestamps, or IntroDB agreeing with them within 2 s
  (identical to the tenth of a second doesn't count: one was likely copied from the other).
- A file 4.3% longer or shorter than the timestamps' (a 25 fps PAL release of a 23.976 fps one) gets them scaled.
"""
import asyncio
import logging
from dataclasses import dataclass, replace

import httpx

log = logging.getLogger(__name__)
THEINTRODB = "https://api.theintrodb.org/v4/media"
INTRODB = "https://api.introdb.app/segments"
KINDS = {"intro": "intro", "recap": "recap", "credits": "credits", "outro": "credits"}
SAME_LENGTH_MS = 2_000      # skipping by itself
NEAR_LENGTH_MS = 20_000     # skipping when asked (the position check does the rest)
PAL = 25 / 23.976


@dataclass(frozen=True)
class Segment:
    kind: str            # "intro", "recap" or "credits"
    start_ms: int
    end_ms: int
    source: str          # "TheIntroDB" or "IntroDB"
    sure: bool = False   # passes the rule for skipping by itself
    same_length: bool = False   # made on a file within 2 s of this one's length (TheIntroDB tells)


def _length_fit(file_ms: int, group_ms: int | None) -> tuple[float, bool, bool] | None:
    """(scale, same length, near length) for timestamps made on a file of group_ms; None when they don't fit."""
    if not group_ms or not file_ms:
        return None
    if abs(file_ms - group_ms) <= NEAR_LENGTH_MS:
        return 1.0, abs(file_ms - group_ms) <= SAME_LENGTH_MS, True
    for ratio in (PAL, 1 / PAL):
        if abs(file_ms / group_ms - ratio) < 0.003:
            return file_ms / group_ms, False, True
    return None


async def _theintrodb(http: httpx.AsyncClient, imdb: str, season: int, episode: int, file_ms: int) -> list[Segment]:
    try:
        r = await http.get(THEINTRODB, params={"imdb_id": imdb, "season": season, "episode": episode,
                                                "duration_ms": file_ms, "details": "true", "merge_unknown": "false"},
                           timeout=5)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("TheIntroDB: %s", type(e).__name__)
        return []
    fit = _length_fit(file_ms, data.get("group_duration_ms"))
    if fit is None:
        return []
    scale, same, _ = fit
    out = []
    for key, kind in KINDS.items():
        found = [x for x in data.get(key) or [] if isinstance(x, dict)]
        if not found:
            continue
        best = max(found, key=lambda x: x.get("submission_count") or 0)
        start = round((best.get("start_ms") or 0) * scale)
        end = round(best["end_ms"] * scale) if best.get("end_ms") is not None else file_ms
        if end - start < 3_000:
            continue
        sure = (same and len(found) == 1 and (best.get("submission_count") or 0) >= 3
                and (best.get("confidence") or 0) >= 0.5)
        out.append(Segment(kind, start, end, "TheIntroDB", sure, same))
    return out


async def _introdb(http: httpx.AsyncClient, imdb: str, season: int, episode: int, file_ms: int) -> list[Segment]:
    try:
        r = await http.get(INTRODB, params={"imdb_id": imdb, "season": season, "episode": episode}, timeout=5)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("IntroDB: %s", type(e).__name__)
        return []
    out = []
    for key, kind in KINDS.items():
        x = data.get(key)
        if not isinstance(x, dict) or x.get("start_ms") is None:
            continue
        start, end = int(x["start_ms"]), int(x["end_ms"]) if x.get("end_ms") is not None else file_ms
        if end - start >= 3_000:
            out.append(Segment(kind, start, end, "IntroDB"))
    return out


async def fetch(http: httpx.AsyncClient, imdb: str, season: int, episode: int, file_ms: int) -> list[Segment]:
    """Every segment either source has for this file, TheIntroDB's first; IntroDB's agreeing with a TheIntroDB one
    within 2 s at both ends makes that one sure."""
    tidb, idb = await asyncio.gather(_theintrodb(http, imdb, season, episode, file_ms),
                                     _introdb(http, imdb, season, episode, file_ms))
    agreed = {s.kind for s in tidb for o in idb if s.same_length and o.kind == s.kind
              and abs(o.start_ms - s.start_ms) <= 2_000 and abs(o.end_ms - s.end_ms) <= 2_000
              and not (abs(o.start_ms - s.start_ms) <= 100 and abs(o.end_ms - s.end_ms) <= 100)}   # copied
    tidb = [replace(s, sure=s.sure or s.kind in agreed) for s in tidb]
    return tidb + idb


def around(segments: list[Segment], kind: str, position_ms: int, lead_ms: int = 15_000) -> Segment | None:
    """The segment of this kind you're in (or about to be in): what "skip the intro" means."""
    for s in segments:
        if s.kind == kind and s.start_ms - lead_ms <= position_ms < s.end_ms - 1_000:
            return s
    return None
