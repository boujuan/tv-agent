"""Addon subtitles timed to the file they're shown over. An addon's subtitles are made for some release, and a
file of another one can run a little earlier or later (a different intro, a studio card cut): every line then shows
too soon or too late by the same amount. That amount is measured by lining the addon's lines up with the file's own
subtitles (episode_check.file_cues: half a minute of them, any language) by when they start: two subtitle files of
one film, even in two languages, start their lines at much the same moments. Only a clear answer counts: enough
lines must meet, and no other offset may come close, so a film whose own subtitles are sparse, or a pattern that
repeats, leaves the subtitles as they were rather than moving them somewhere wrong."""
import statistics

MAX_SHIFT = 10_000          # ms either way: more than this is another cut, not a timing difference
NEAR = 300                  # ms: a line of theirs this close to one of the file's after the shift is the same line
STEP = 100                  # ms: offsets this close count as one
MIN_LINES = 5               # the file's lines that must meet one of theirs
SHARE = 0.6                 # and at least this share of the file's lines
CHANCE = 3                  # and this many more than lines that dense would meet by chance
RIVAL = 0.6                 # another offset (a second or more away) meeting this share of as many lines: unclear
SPREAD = 250                # ms: how far the met lines may be from the offset found, on the median


def shift(found: list[tuple[int, str]], theirs: list[tuple[int, str]]) -> int | None:
    """How many ms later `theirs` run than `found` (the file's own lines, in its time): None unless clear."""
    mine = sorted(t for t, _ in found)
    if len(mine) < MIN_LINES:
        return None
    lo, hi = mine[0] - MAX_SHIFT - NEAR, mine[-1] + MAX_SHIFT + NEAR
    near = sorted(t for t, _ in theirs if lo <= t <= hi)
    if len(near) < MIN_LINES:
        return None
    # Every offset that puts one of their lines on one of the file's, then how many of the file's lines each meets
    offsets = {round((b - a) / STEP) * STEP for a in mine for b in near if abs(b - a) <= MAX_SHIFT}
    scored = sorted(((_met(mine, near, d), -abs(d), d) for d in offsets), reverse=True)
    if not scored:
        return None
    best_lines, _, best = scored[0]
    rival = max((n for n, _, d in scored if abs(d - best) >= 1000), default=0)
    # Their lines that dense meet a share of the file's at any offset: a match must stand out from that
    chance = len(mine) * min(1.0, 2 * NEAR * len(near) / max(1, near[-1] - near[0]))
    if (best_lines < MIN_LINES or best_lines < SHARE * len(mine) or best_lines < chance + CHANCE
            or rival > RIVAL * best_lines):
        return None
    pairs = _pairs(mine, near, best)
    exact = round(statistics.median(b - a for a, b in pairs))
    if statistics.median(abs(b - a - exact) for a, b in pairs) > SPREAD or abs(exact) > MAX_SHIFT:
        return None
    return exact


def _pairs(mine: list[int], near: list[int], d: int) -> list[tuple[int, int]]:
    """(the file's line, the nearest of theirs) for each of the file's lines that one of theirs meets at offset d."""
    out = []
    for a in mine:
        b = min(near, key=lambda t: abs(t - (a + d)))
        if abs(b - (a + d)) <= NEAR:
            out.append((a, b))
    return out


def _met(mine: list[int], near: list[int], d: int) -> int:
    return len(_pairs(mine, near, d))
