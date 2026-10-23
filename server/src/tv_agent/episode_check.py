"""Is a file the episode it's named? A streaming service's rips follow the service's numbering: Netflix dropped
Community's "Advanced Dungeons & Dragons" (S2E14) in 2020, so the Netflix rips' "S02E14" is the aired 15th and every
later one moves up by one, while Blu-ray rips keep the aired order (found 2026-10-23). Half a minute of the file's
own subtitles (3-4 s to read, even in 4K: ffmpeg seeks with the file's index) is compared with the episode's
subtitles from your subtitle addons, asked by episode id (never by the file's hash: that would describe the file
itself), and with the neighbouring episodes' when they don't match. What's found is kept, so a file is checked once.
The comparison also gives how far those subtitles' times are from this file's, for jumps to a moment."""
import asyncio
import json
import logging
import re
import statistics
import time
from dataclasses import asdict, dataclass

from . import config as config_mod
from .stremio import subtitles

log = logging.getLogger("tv_agent")
WINDOW = (60, 25)           # seconds into the file, and how many: past most cold opens' first lines
FAST = ["-probesize", "1000000", "-analyzeduration", "0"]   # an MKV lists its tracks in its header: no more needed
NEAR = 150                  # the episode's subtitles this far either side count (intros and cuts move lines)
MATCH = 0.45                # share of the file's words found near there in the episode's subtitles: the same one
UNLIKE = 0.2                # below this, with no neighbour matching either, it's still not that episode
TEXT = ("subrip", "ass", "ssa", "webvtt", "mov_text", "text")
WORD = re.compile(r"[^\W\d_]{4,}")
# Words in any episode: they say nothing about which one (English and Spanish)
COMMON = set("""that this with what have just your from they there were will would about know like right well yeah
here then them when want okay think could going been doing come some really because thing things where gonna very
something mean back over time look good only even into need tell said these those more said much make take sure
para pero como esto esta este todo nada bien porque cuando donde quiero tengo tiene puedo hacer creo sabes
""".split())


def words(text: str) -> set[str]:
    return {w for w in (x.lower() for x in WORD.findall(text)) if w not in COMMON}


def compare(found: list[tuple[int, str]], episode: list[tuple[int, str]]) -> tuple[float, float | None]:
    """(share of the file's words in the episode's subtitles near the same time, how many seconds later the
    episode's subtitles have the same lines: None without two clear pairs)."""
    if not found:
        return 0.0, None
    lo, hi = found[0][0] - NEAR * 1000, found[-1][0] + NEAR * 1000
    near = [(t, words(x)) for t, x in episode if lo <= t <= hi]
    mine = set().union(*(words(x) for _, x in found))
    theirs = set().union(*(w for _, w in near)) if near else set()
    share = len(mine & theirs) / len(mine) if mine else 0.0
    shifts = []
    for t, x in found:
        w = words(x)
        if len(w) < 2:
            continue
        best = max(near, key=lambda r: len(w & r[1]), default=None)
        if best and len(w & best[1]) >= 0.8 * len(w):
            shifts.append((best[0] - t) / 1000)
    return share, (statistics.median(shifts) if len(shifts) >= 2 else None)


async def _run(argv: list[str], timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()                             # a read no longer needed doesn't go on downloading
        raise
    return out.decode("utf-8", errors="replace")


def _read(url: str, stream: str, start: int, length: int) -> asyncio.Task:
    return asyncio.create_task(_run(["ffmpeg", "-v", "error", *FAST, "-ss", str(start), "-i", url, "-map", stream,
                                     "-t", str(length), "-f", "srt", "-"], 40))


async def file_cues(url: str, start: int = WINDOW[0], length: int = WINDOW[1]) -> tuple[str, list] | None:
    """(language, cues in the file's own time) of its English text subtitles (else its first text ones) from
    `start` for `length` seconds; None when the file has no text subtitles (picture-based or none). The first
    subtitle track, the one wanted in most files, is read while the tracks are listed."""
    first = _read(url, "0:s:0", start, length)
    try:
        info = json.loads(await _run(["ffprobe", "-v", "error", *FAST, "-show_entries",
                                      "stream=index,codec_type,codec_name:stream_tags=language,title", "-of", "json",
                                      url], 20) or "{}")
        subs = [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]
        text = [s for s in subs if s.get("codec_name") in TEXT]
        if not text:
            first.cancel()
            return None
        lang = lambda s: str((s.get("tags") or {}).get("language", ""))           # noqa: E731
        plain = lambda s: not subtitles.IMPAIRED.search(str((s.get("tags") or {}).get("title", "")))   # noqa: E731
        pick = (next((s for s in text if lang(s).startswith("en") and plain(s)), None)
                or next((s for s in text if lang(s).startswith("en")), None) or text[0])
        if pick is subs[0]:
            out = await first
        else:
            first.cancel()
            out = await _read(url, f"0:{pick['index']}", start, length)
    except BaseException:
        first.cancel()
        raise
    return (lang(pick) or "eng"), [(ms + start * 1000, x) for ms, x in subtitles.cues(out)]   # ffmpeg counts from 0


@dataclass
class Verdict:
    video: str                      # the episode asked for
    ok: bool | None                 # True: it is that one; False: it isn't; None: couldn't tell
    actual: str | None = None       # the episode it is instead, when a neighbour matched
    shift: float | None = None      # seconds the addon subtitles run later than this file
    share: float = 0.0
    checked: float = 0.0


class Checks:
    """What's known about files (by name, size and the episode asked for): whether each is that episode."""

    def __init__(self, path=None):
        self.path = path or config_mod.DATA_DIR / "episode-checks.json"
        try:
            self.files = {k: Verdict(**v) for k, v in json.loads(self.path.read_text()).items()}
        except (OSError, ValueError, TypeError):
            self.files = {}

    @staticmethod
    def key(filename: str, size: int, video: str) -> str:
        return f"{filename}|{size}|{video}"

    def get(self, filename: str, size: int, video: str) -> Verdict | None:
        return self.files.get(self.key(filename, size, video))

    def playing_as(self, video: str) -> list[tuple[str, int, str]]:
        """Files named as another episode that are this one: (filename, size, the episode they're named)."""
        out = []
        for k, v in self.files.items():
            if v.ok is False and v.actual == video:
                filename, size, _ = k.rsplit("|", 2)
                out.append((filename, int(size), v.video))
        return out

    def wrong(self, filename: str, size: int, video: str) -> Verdict | None:
        """A verdict that this file isn't `video`."""
        v = self.get(filename, size, video)
        return v if v and v.ok is False else None

    def keep(self, filename: str, size: int, verdict: Verdict):
        if not filename:
            return
        self.files[self.key(filename, size, verdict.video)] = verdict
        if len(self.files) > 5000:              # the oldest go
            for k in sorted(self.files, key=lambda k: self.files[k].checked)[:1000]:
                del self.files[k]
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({k: asdict(v) for k, v in self.files.items()}, ensure_ascii=False))
            tmp.replace(self.path)
        except OSError as e:
            log.info("episode checks: %s", type(e).__name__)


def neighbours(video: str, ids: list[str]) -> list[str]:
    """The episodes next to this one (in the show's list, which crosses seasons), nearest first."""
    if video not in ids:
        return []
    i = ids.index(video)
    return [ids[j] for d in (1, -1, 2, -2) if 0 <= (j := i + d) < len(ids)]


async def check(url: str, video: str, episode_subs, ids: list[str]) -> Verdict:
    """Whether the file at url is `video`. episode_subs(video_id, lang) gives an episode's subtitle cues (asked by
    id). The file's lines and the episode's subtitles are fetched at once, the neighbours' all together."""
    reading = asyncio.create_task(file_cues(url))
    english = asyncio.create_task(episode_subs(video, "eng"))       # what most files have: asked meanwhile
    try:
        got = await reading
    except BaseException:
        english.cancel()
        raise
    if not got or len(got[1]) < 3:
        english.cancel()
        return Verdict(video, None, checked=time.time())
    lang, found = got
    mine = await english if lang == "eng" else await episode_subs(video, lang)
    if len(mine) < 20:
        return Verdict(video, None, checked=time.time())
    share, shift = compare(found, mine)
    if share >= MATCH:
        return Verdict(video, True, shift=shift, share=round(share, 2), checked=time.time())
    others = neighbours(video, ids)
    for other, theirs in zip(others, await asyncio.gather(*(episode_subs(o, lang) for o in others))):
        their_share, _ = compare(found, theirs)
        if len(theirs) >= 20 and their_share >= MATCH and their_share > share + 0.2:
            return Verdict(video, False, actual=other, share=round(share, 2), checked=time.time())
    return Verdict(video, False if share < UNLIKE else None, share=round(share, 2), checked=time.time())
