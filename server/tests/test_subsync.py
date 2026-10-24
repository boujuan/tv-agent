"""Addon subtitles lined up with the file's own (subsync.py): by when lines start, in any language."""
import random

from tv_agent import subsync

# Half a minute of a film's own subtitles from 60 s, at uneven moments as dialogue is
FILE = [(t, "line") for t in (61200, 63900, 66100, 70500, 72300, 75800, 78000, 80900, 83400, 84600)]


def theirs(shift_ms: int, jitter: int = 120, seed: int = 1, extra: int = 4) -> list[tuple[int, str]]:
    """Another subtitle file of the same film, `shift_ms` later, its lines a little off and split differently."""
    rnd = random.Random(seed)
    out = [(t + shift_ms + rnd.randint(-jitter, jitter), "línea") for t, _ in FILE]
    out += [(rnd.randint(55_000, 90_000) + shift_ms, "otra") for _ in range(extra)]      # lines the file splits
    return sorted(out + [(t, "lejos") for t in range(0, 50_000, 3_700)])                  # the rest of the film


def test_a_constant_offset_is_found_either_way():
    for d in (1700, -500, 4200, -8800):
        found = subsync.shift(FILE, theirs(d))
        assert found is not None and abs(found - d) <= 100, (d, found)


def test_lines_already_in_time_give_about_zero():
    assert abs(subsync.shift(FILE, theirs(0))) <= 100


def test_too_few_of_the_files_lines_leave_it_alone():
    assert subsync.shift(FILE[:4], theirs(1500)) is None


def test_a_pattern_that_repeats_is_not_clear():
    regular = [(60_000 + i * 2_000, "x") for i in range(12)]
    assert subsync.shift(regular, [(t + 700, "y") for t, _ in regular]) is None   # every 2 s fits as well


def test_more_than_ten_seconds_or_unrelated_lines_find_nothing():
    assert subsync.shift(FILE, theirs(15_000)) is None
    rnd = random.Random(7)
    assert subsync.shift(FILE, sorted((rnd.randint(40_000, 100_000), "z") for _ in range(30))) is None


def test_the_agent_tells_the_player_a_clear_shift_and_keeps_it(monkeypatch):
    """Measured once after the start and sent as that track's delay (the opposite sign: theirs run later, so they're
    shown sooner); kept for the file, and a file whose lines don't meet them gets nothing."""
    import asyncio
    import types
    from tv_agent import agent as agent_mod, episode_check
    from tv_agent.stremio import subtitles
    monkeypatch.setattr(agent_mod, "CHECK_AFTER", 0)
    sent = []

    async def subtitle_delay(index, ms):
        sent.append((index, ms))
    a = agent_mod.Agent.__new__(agent_mod.Agent)
    np = types.SimpleNamespace(filename="Show.S01E01.mkv", size=1)
    a.state = types.SimpleNamespace(now_playing=np)
    a.tvplayer = types.SimpleNamespace(subtitle_delay=subtitle_delay)
    a.http = None
    a._sub_shifts = {}
    sub = subtitles.Subtitle("https://subs.example/1.srt", "spa", "Spanish", "OpenSubtitles")

    async def file_cues(link):
        return "eng", FILE

    async def download(http, url):
        return "srt"
    monkeypatch.setattr(episode_check, "file_cues", file_cues)
    monkeypatch.setattr(subtitles, "download", download)
    monkeypatch.setattr(subtitles, "cues", lambda text: theirs(2300))
    asyncio.run(a._sync_subtitle(np, 3, sub, "spa", "link", ("Show.S01E01.mkv", 1, "k")))
    assert len(sent) == 1 and sent[0][0] == 3 and abs(sent[0][1] + 2300) <= 100
    assert abs(a._sub_shifts[("Show.S01E01.mkv", 1, "k")] - 2300) <= 100
    monkeypatch.setattr(subtitles, "cues", lambda text: [(t, "x") for t in range(60_000, 90_000, 2_000)])
    asyncio.run(a._sync_subtitle(np, 4, sub, "spa", "link", ("Show.S01E01.mkv", 1, "other")))
    assert len(sent) == 1 and a._sub_shifts[("Show.S01E01.mkv", 1, "other")] is None
