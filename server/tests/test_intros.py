"""Intros, recaps and credits found in the files (intros.py): chapters, release groups' votes, the sound."""
import asyncio
import types

import numpy as np

from tv_agent import actions as A
from tv_agent import intros as I
from tv_agent.agent import Agent
from tv_agent.skips import Segment
from tv_agent.state import NowPlaying

RATE = I.RATE


# ---- chapters ----

def ebml(eid: int, data: bytes) -> bytes:
    """An EBML element (its ID as written, an 8-byte size)."""
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big") + b"\x01" + len(data).to_bytes(7, "big") + data


def atom(start_ms: int, name: str, hidden: bool = False) -> bytes:
    body = ebml(I.CHAPTER_START, (start_ms * 1_000_000).to_bytes(8, "big"))
    if hidden:
        body += ebml(I.HIDDEN, b"\x01")
    return ebml(I.ATOM, body + ebml(I.DISPLAY, ebml(I.CHAP_STRING, name.encode())))


def chapters_element(*editions: list[bytes], default: int | None = None) -> bytes:
    out = b""
    for i, atoms in enumerate(editions):
        flag = ebml(I.EDITION_DEFAULT, b"\x01") if i == default else b""
        out += ebml(I.EDITION, flag + b"".join(atoms))
    return ebml(I.CHAPTERS, out)


def test_chapters_are_read_from_the_default_edition_without_hidden_ones():
    b = chapters_element([atom(0, "Other")],
                         [atom(216_500, "Opening Credit"), atom(233_000, "Chapter 01"), atom(500_000, "x", True),
                          atom(2_775_000, "End Credits")], default=1)
    assert I.chapters(b) == [I.Chapter(216_500, "Opening Credit"), I.Chapter(233_000, "Chapter 01"),
                             I.Chapter(2_775_000, "End Credits")]


def test_chapter_names_mark_the_intro_recap_and_credits_as_the_services_name_them():
    C = I.Chapter
    # Netflix (Community S2E6): the intro to the next chapter, the credits to the end of the file
    nf = I.from_chapters([C(246_308, "Opening Credit"), C(270_938, "Chapter 01"), C(1_270_000, "End Credits")],
                         1_283_744)
    assert [(s.kind, s.start_ms, s.end_ms, s.source, s.sure) for s in nf] == [
        ("intro", 246_308, 270_938, "chapters", False), ("credits", 1_270_000, 1_283_744, "chapters", False)]
    # Apple TV+ through FLUX: logo, recap, intro, credits
    atvp = I.from_chapters([C(0, "Studio Logo"), C(8_008, "Recap"), C(49_383, "Scene 01"), C(439_222, "Intro"),
                            C(464_181, "Scene 03"), C(2_489_041, "Credits")], 2_582_000)
    assert [(s.kind, s.start_ms, s.end_ms) for s in atvp] == [
        ("recap", 8_008, 49_383), ("intro", 439_222, 464_181), ("credits", 2_489_041, 2_582_000)]
    # Amazon's scene names: the intro is the chapter named after the show, in the first quarter
    amzn = I.from_chapters([C(0, "Walt and Jesse"), C(161_000, "A Supermarket Run"), C(216_000, "Breaking Bad"),
                            C(234_000, "Skyler and Walt"), C(2_775_000, "End Credits")], 2_822_780, "Breaking Bad")
    assert [(s.kind, s.start_ms, s.end_ms) for s in amzn] == [("intro", 216_000, 234_000),
                                                             ("credits", 2_775_000, 2_822_780)]
    # Numbered chapters (a Blu-ray's), credits too early to be the end's, an "intro" of 5 minutes: nothing
    assert I.from_chapters([C(0, "Chapter 1"), C(234_025, "00:03:54.025"), C(2_775_189, "06")], 2_819_660) == []
    assert I.from_chapters([C(60_000, "Credits"), C(90_000, "Chapter 2")], 2_800_000) == []
    assert I.from_chapters([C(60_000, "Intro"), C(360_000, "Chapter 2")], 2_800_000) == []


# ---- fingerprints ----

def music(seconds: float, seed: int = 7) -> np.ndarray:
    """A theme: sound across the bands (a band left empty would hold only noise, different in each episode), its
    loudness and notes changing as a tune does; the same for the same seed."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    notes = rng.uniform(300, 1800, int(seconds * 4) + 1)
    f = np.repeat(notes, RATE // 4 + 1)[:n]
    beat = np.repeat(rng.uniform(0.3, 1.0, int(seconds * 8) + 1), RATE // 8 + 1)[:n]
    return ((2000 * np.sin(2 * np.pi * np.cumsum(f) / RATE) + rng.normal(0, 1500, n)) * beat).astype(np.float32)


def episode(seed: int, intro_at: float | None, seconds: float = 360, gain: float = 1.0,
            intro_s: float = 30) -> np.ndarray:
    """An episode's sound: its own (noise shaped like speech and scenes), the season's intro where it is."""
    rng = np.random.default_rng(seed)
    out = (rng.normal(0, 1500, int(seconds * RATE)) * np.repeat(rng.uniform(0.2, 1.5, int(seconds) + 1), RATE)
           [:int(seconds * RATE)]).astype(np.float32)
    if intro_at is not None:
        a = int(intro_at * RATE)
        theme = music(intro_s)
        out[a:a + len(theme)] = theme * gain + rng.normal(0, 60, len(theme))
    return out


def test_the_same_sound_is_found_wherever_it_is_and_however_loud():
    a = I.fingerprint(episode(1, 100))
    b = I.fingerprint(episode(2, 47.3, gain=0.4))
    m = I.shared(a, b)[0]
    assert abs(a.ms(m.a) - 100_000) < 500 and abs(b.ms(m.b) - 47_300) < 500
    assert abs(m.seconds - 30) < 1.5 and m.err < 6
    # unrelated sound shares nothing, and neither does silence
    assert I.shared(I.fingerprint(episode(3, None)), I.fingerprint(episode(4, None))) == []
    quiet = np.zeros(60 * RATE, np.float32)
    assert I.shared(I.fingerprint(quiet), I.fingerprint(quiet)) == []


def test_a_print_keeps_and_comes_back_the_same():
    p = I.fingerprint(episode(1, 30, seconds=90))
    q = I.Print.unpack(p.pack())
    assert np.array_equal(p.bits, q.bits) and np.array_equal(p.quiet, q.quiet) and abs(p.t0 - q.t0) < 0.1


def test_a_season_s_intro_is_what_its_episodes_share():
    prints = [I.fingerprint(episode(10, 95)), I.fingerprint(episode(11, 246)), I.fingerprint(episode(12, 178))]
    ref, starts = I.season_intro(prints)
    assert abs(len(ref) * I.STEP_MS - 30_000) < 1500
    assert [abs(s - t) < 700 for s, t in zip(starts, (95_000, 246_000, 178_000))] == [True] * 3
    # one episode without it (Community's bottle episode): found in the others, none in it
    prints[2] = I.fingerprint(episode(13, None))
    assert I.season_intro(prints) is None
    ref, starts = I.season_intro(prints, 2)
    assert starts[2] is None and abs(starts[0] - 95_000) < 700
    # a studio's logo at the very start, shared by all: not an intro
    logo = [episode(20 + k, 0, intro_s=9) for k in range(3)]
    assert I.season_intro([I.fingerprint(x) for x in logo]) is None


def test_two_of_three_must_share_a_long_clean_stretch():
    """A recap shows scenes of the last episode: two that share only some seconds of sound aren't an intro."""
    a, b, c = episode(30, 60, intro_s=10), episode(31, 120, intro_s=10), episode(32, None)
    assert I.season_intro([I.fingerprint(a), I.fingerprint(b), I.fingerprint(c)], 2) is None


# ---- release groups' votes ----

def test_one_group_s_chapters_are_outvoted_by_two_others():
    """Pluribus S1E3 (measured 2026-10-24): FLUX's three releases put the intro at 7:19, Kitsune and NTb at 6:23,
    where the season's theme is."""
    flux = Segment("intro", 439_222, 464_181, "chapters")
    right = Segment("intro", 383_791, 408_750, "chapters of another copy")
    fl = ("flux", ((0, "Studio Logo"), (439_222, "Intro")))
    v = lambda src, seg, sig=None: (src, sig or (src,), seg)                          # noqa: E731
    best, sure = I.vote((*fl, flux), [(*fl, flux), (*fl, flux), v("kitsune", right), v("ntb", right)])
    assert best == right and sure
    assert I.vote((*fl, flux), [(*fl, flux)]) == (flux, False)          # one group: not sure
    assert I.vote(v("ihatepeaches", flux), []) == (flux, False)
    assert I.vote(None, []) == (None, False)
    near = Segment("intro", 384_500, 409_000, "chapters")
    assert I.vote(v("a", near), [v("b", right)]) == (near, True)               # within 2 s: they agree, mine kept
    assert I.group("Pluribus S01E03 Grenade 1080p ATVP WEB-DL DDP5 1 Atmos H 264-FLUX.mkv") == "flux"


def test_copies_with_the_same_chapter_list_are_one_source_whatever_their_name():
    """On the TV (2026-10-25) FLUX's wrong chapters won once: a second copy carried the very same chapter list under
    another name. The same list, to the millisecond, is one source."""
    wrong = Segment("credits", 2_489_041, 2_582_000, "chapters")
    right = Segment("credits", 2_521_916, 2_582_000, "chapters")
    sig = ((0, "Studio Logo"), (8_008, "Recap"), (2_489_041, "Credits"))
    best, sure = I.vote(("kitsune", ("k",), right), [("flux", sig, wrong), ("pluribus.s01e03.2160p", sig, wrong)])
    assert best == right and not sure                    # one against one: this file's, not sure
    assert I.sources([("a", ("x",), wrong), ("b", ("x",), wrong), ("b", ("y",), wrong), ("c", ("z",), wrong)]) == 2


# ---- the finder, on fake files ----

class FakeFile:
    """A file of an episode: its header facts and its sound (an episode() array)."""

    def __init__(self, name: str, size: int, duration_ms: int, chapters=(), pcm=None, mbit: float = 5.0):
        self.name, self.size, self.pcm = name, size, pcm
        mkv = types.SimpleNamespace(duration_ms=duration_ms, places=[(0, None, 0), (int(mbit * 125_000 * 100), None,
                                                                                     100_000)], times=[0, 100_000])
        self.head = I.Header(mkv, list(chapters), [(2, "eng", True)])
        self.reads: list[tuple[int, int]] = []


def finder(tmp_path, files_by_video: dict[str, list[FakeFile]], monkeypatch):
    f = I.Finder(None, I.Store(tmp_path))
    by_reader = {}

    def make(ff: FakeFile) -> I.Copy:
        c = I.Copy(ff.name, ff.size, I.Reader(None, "https://debrid.example/KEY/" + ff.name))
        by_reader[id(c.reader)] = ff
        return c

    async def header(reader):
        return by_reader[id(reader)].head

    async def sound(reader, h, t0, t1, track=0):
        ff = by_reader[id(reader)]
        ff.reads.append((t0, t1))
        reader.read += int((t1 - t0) / 1000 * 5e6 / 8)
        return ff.pcm[int(t0 / 1000 * RATE):int(t1 / 1000 * RATE)], t0
    monkeypatch.setattr(I, "header", header)
    monkeypatch.setattr(I, "sound", sound)

    async def copies(video):
        async def opener(ff=None):
            return make(ff)
        return [(ff.name, ff.size, (lambda ff=ff: opener(ff))) for ff in sorted(files_by_video.get(video, []),
                                                                                  key=lambda x: x.size)]
    return f, make, copies


def test_chapters_two_groups_agree_on_are_sure_and_nothing_is_listened_to(tmp_path, monkeypatch):
    C = I.Chapter
    ch = [C(383_791, "Intro"), C(408_750, "Chapter 2"), C(2_521_916, "End Credits")]
    playing = FakeFile("Pluribus S01E03 2160p ATVP WEB-DL DV-NTb.mkv", 8_160_000_000, 2_582_000, ch)
    other = FakeFile("Pluribus S01E03 REPACK 2160p ATVP WEB-DL-RAWR.mkv", 8_180_000_000, 2_582_040,
                     [C(0, "Chapter 1"), C(383_800, "Intro"), C(408_800, "Chapter 3"), C(2_522_000, "End Credits")])
    flux = FakeFile("Pluribus S01E03 1080p ATVP WEB-DL-FLUX.mkv", 3_480_000_000, 2_582_000,
                    [C(439_222, "Intro"), C(464_181, "Scene 03"), C(2_489_041, "Credits")])
    f, make, copies = finder(tmp_path, {"tt2:1:3": [playing, other, flux]}, monkeypatch)
    me = make(playing)
    found = asyncio.run(f.find("tt2", "tt2:1:3", "Pluribus", me, copies, ["tt2:1:4", "tt2:1:2"]))
    assert [(s.kind, s.start_ms, s.end_ms, s.sure) for s in found] == [
        ("intro", 383_791, 408_750, True), ("credits", 2_521_916, 2_582_000, True)]
    assert not playing.reads and not other.reads and not flux.reads            # headers only
    assert f.known("tt2", playing.name, playing.size) == found                 # kept
    # the FLUX copy played: its chapters outvoted, the other groups' times instead
    found = asyncio.run(f.find("tt2", "tt2:1:3", "Pluribus", make(flux), copies, []))
    assert [(s.kind, s.sure) for s in found] == [("intro", True), ("credits", True)]
    assert abs(found[0].start_ms - 383_791) <= 100 and abs(found[1].start_ms - 2_521_916) <= 100
    assert "KEY" not in str(f.store.title("tt2"))                              # no link kept


def test_a_service_that_marks_credits_and_no_intro_means_there_is_none(tmp_path, monkeypatch):
    C = I.Chapter
    dsnp = FakeFile("The Bear S02E03 2160p DSNP WEB-DL-FLUX.mkv", 2_110_000_000, 1_563_100, [C(1_517_433, "Credits")])
    hulu = FakeFile("The Bear S02E03 2160p HULU WEB-DL-NTb.mkv", 3_020_000_000, 1_563_200, [C(1_517_200, "Credits")])
    f, make, copies = finder(tmp_path, {"tt3:2:3": [dsnp, hulu]}, monkeypatch)
    found = asyncio.run(f.find("tt3", "tt3:2:3", "The Bear", make(dsnp), copies, ["tt3:2:4"]))
    assert [(s.kind, s.start_ms, s.sure) for s in found] == [("credits", 1_517_433, True)]
    assert not dsnp.reads and not hulu.reads


def test_with_no_chapters_the_season_s_sound_finds_the_intro_and_a_later_episode_reads_little(tmp_path, monkeypatch):
    eps = {f"tt4:1:{e}": [FakeFile(f"Show.S01E0{e}.1080p.BluRay.x264-GRP.mkv", 1_000_000_000 + e, 360_000,
                                   pcm=episode(40 + e, at))] for e, at in ((2, 95), (3, 70), (4, 52), (5, 88))}
    f, make, copies = finder(tmp_path, eps, monkeypatch)
    me = eps["tt4:1:3"][0]
    found = asyncio.run(f.find("tt4", "tt4:1:3", "Show", make(me), copies, ["tt4:1:4", "tt4:1:2"]))
    intro = next(s for s in found if s.kind == "intro")
    assert intro.source == "sound" and intro.sure
    assert abs(intro.start_ms - 70_000) < 700 and abs(intro.end_ms - 100_000) < 1500
    assert f.store.reference("tt4", 1, "intro") is not None
    # the next episode: only its own start is read, a part at a time, and only until the intro
    nxt = eps["tt4:1:5"][0]
    before = len(eps["tt4:1:2"][0].reads)
    found = asyncio.run(f.find("tt4", "tt4:1:5", "Show", make(nxt), copies, ["tt4:1:6", "tt4:1:4"]))
    intro = next(s for s in found if s.kind == "intro")
    assert abs(intro.start_ms - 88_000) < 700
    assert nxt.reads[0][0] == 0 and nxt.reads[0][1] <= I.SCAN_MS + 40_000
    assert len(eps["tt4:1:2"][0].reads) == before          # the others aren't read again


def test_a_wrong_intro_chapter_is_replaced_by_where_the_sound_is(tmp_path, monkeypatch):
    """One group's chapter, and the season's intro sound known: a short read where the chapter says, not there,
    so the episode's start is read for it."""
    C = I.Chapter
    pcm = episode(51, 70)
    me = FakeFile("Show.S01E03.1080p.WEB-DL-FLUX.mkv", 1_000, 360_000, [C(125_000, "Intro"), C(155_000, "Scene")],
                  pcm=pcm)
    f, make, copies = finder(tmp_path, {"tt5:1:3": [me]}, monkeypatch)
    ref = I.fingerprint(music(30), 0)
    f.store.season("tt5", 1)["intro"] = {"print": ref.pack(), "ms": 30_000}
    found = asyncio.run(f.find("tt5", "tt5:1:3", "Show", make(me), copies, []))
    intro = next(s for s in found if s.kind == "intro")
    assert intro.source == "sound" and abs(intro.start_ms - 70_000) < 700
    assert me.reads[0] == (122_000, 125_000 + I.PROBE_MS)        # the probe where the chapter said


# ---- the agent ----

def test_markers_found_in_the_file_come_before_the_databases_and_skip_when_asked():
    seeks, watched = [], []
    pos = {"ms": 30_000}

    async def position(pid):
        return pos["ms"], 2_582_000, 1.0

    async def seek_to(pid, ms):
        seeks.append(ms)

    async def player():
        return "tvplayer"
    own = Segment("intro", 383_791, 408_750, "chapters of another copy", True, True)
    np_ = NowPlaying("tt2", "series", "tt2:1:3", "Pluribus", player="tvplayer", filename="P.mkv", size=1)
    ctl = types.SimpleNamespace(position=position, seek_to=seek_to)

    async def segments(np, total):
        return [own]
    me = types.SimpleNamespace(state=types.SimpleNamespace(now_playing=np_), _player_id=player, segments=segments,
                               _ctl=lambda pid: ctl, cfg=types.SimpleNamespace(binge=False),
                               _start_skip_watch=lambda np, also=None: watched.append(also))
    out = asyncio.run(Agent._skip(me, A.Skip(what="intro")))      # in the cold open: when it comes
    assert out.ok and out.say == "The intro starts at 6:23: skipping it then" and watched == ["intro"] and not seeks
    pos["ms"] = 390_000
    out = asyncio.run(Agent._skip(me, A.Skip(what="intro")))
    assert out.say == "Skipped the intro" and seeks == [408_750]
    pos["ms"] = 900_000
    out = asyncio.run(Agent._skip(me, A.Skip(what="intro")))
    assert out.say == "Already past the intro" and seeks == [408_750]


def test_segments_are_the_file_s_own_then_the_databases_for_the_rest(monkeypatch, tmp_path):
    from tv_agent import agent as agent_mod
    a = Agent.__new__(Agent)
    a.intros = I.Finder(None, I.Store(tmp_path))
    a._segments = {}
    np_ = NowPlaying("tt2", "series", "tt2:1:3", "Pluribus", filename="P.mkv", size=1)
    a.intros.store.keep_file("tt2", "tt2:1:3", "P.mkv", 1, 2_582_000,
                             [Segment("intro", 383_791, 408_750, "chapters", True, True)])

    async def fetch(http, imdb, season, ep, total):
        return [Segment("intro", 1_000, 20_000, "TheIntroDB", True), Segment("recap", 8_000, 49_000, "IntroDB")]
    monkeypatch.setattr(agent_mod.skips, "fetch", fetch)
    a.http = None
    got = asyncio.run(Agent.segments(a, np_, 2_582_000))
    assert [(s.kind, s.source) for s in got] == [("intro", "chapters"), ("recap", "IntroDB")]
    assert [s.kind for s in asyncio.run(Agent.segments(a, np_, 0))] == ["intro"]     # length not known yet


def test_the_player_gets_markers_and_the_next_episode_only_when_it_shows_them():
    sent = []
    player = types.SimpleNamespace(has_markers=True, now=lambda: (10_000, 2_582_000, 1.0),
                                   set_markers=lambda m, n: sent.append((m, n)))
    np_ = NowPlaying("tt2", "series", "tt2:1:3", "Pluribus", player="tvplayer")
    found = [Segment("intro", 383_791, 408_750, "chapters", True, True),
             Segment("recap", 8_008, 49_383, "chapters", False, True),
             Segment("credits", 2_521_916, 2_582_000, "chapters", True, True),
             Segment("intro", 1, 2, "IntroDB", False)]

    async def segments(np, total):
        return found
    me = types.SimpleNamespace(state=types.SimpleNamespace(now_playing=np_), tvplayer=player, segments=segments,
                               cfg=types.SimpleNamespace(auto_skip=True, binge=True),
                               _next_up=("tt2:1:4", "Pluribus S1E4", object(), 0.0))
    me._marker_list = lambda segs: Agent._marker_list(me, segs)
    found[2] = Segment("credits", 2_521_916, 2_582_000, "chapters", True, True)
    asyncio.run(Agent._send_markers(me, np_))
    markers, nxt = sent[-1]
    assert markers == [{"kind": "intro", "start": 383_791, "end": 408_750, "auto": True},
                       {"kind": "recap", "start": 8_008, "end": 49_383, "auto": False},   # one group's: a button
                       {"kind": "credits", "start": 2_521_916, "end": 2_582_000, "auto": False}]
    assert nxt == {"title": "Pluribus S1E4", "countdown_ms": 10_000}
    me.cfg.binge, me.cfg.auto_skip = False, False
    asyncio.run(Agent._send_markers(me, np_))
    assert sent[-1][1]["countdown_ms"] == 0 and not any(m["auto"] for m in sent[-1][0])
    player.has_markers = False
    asyncio.run(Agent._send_markers(me, np_))
    assert len(sent) == 2                                     # an older player: nothing sent


def test_the_next_episode_card_accepted_starts_the_next_episode():
    started = []
    np_ = NowPlaying("tt2", "series", "tt2:1:3", "Pluribus", player="tvplayer")

    async def next_episode(a):
        started.append(a.offset)

    async def run():
        me = types.SimpleNamespace(state=types.SimpleNamespace(now_playing=np_),
                                   tvplayer=types.SimpleNamespace(item="tt2:1:3#a-1"), _next_episode=next_episode)
        Agent._player_event(me, {"t": "next_episode", "item": "tt2:1:3#a-1", "how": "countdown"})
        Agent._player_event(me, {"t": "next_episode", "item": "tt2:1:2#a-0", "how": "button"})   # an old item's
        await asyncio.sleep(0)
    asyncio.run(run())
    assert started == [1]


def test_one_group_s_chapter_is_skipped_inside_it_but_never_waited_for():
    """An intro only one release's chapters name (FLUX's can be a minute off): "skip the intro" jumps when you're in
    it, and otherwise goes 90 s ahead as with no times, rather than skipping story when the chapter comes."""
    seeks, watched = [], []

    async def position(pid):
        return 30_000, 2_582_000, 1.0

    async def seek_to(pid, ms):
        seeks.append(ms)

    async def player():
        return "tvplayer"
    unsure = Segment("intro", 439_222, 464_181, "chapters", False, True)
    np_ = NowPlaying("tt2", "series", "tt2:1:3", "Pluribus", player="tvplayer")

    async def segments(np, total):
        return [unsure]
    me = types.SimpleNamespace(state=types.SimpleNamespace(now_playing=np_), _player_id=player, segments=segments,
                               _ctl=lambda pid: types.SimpleNamespace(position=position, seek_to=seek_to),
                               cfg=types.SimpleNamespace(binge=False),
                               _start_skip_watch=lambda np, also=None: watched.append(also))
    out = asyncio.run(Agent._skip(me, A.Skip(what="intro")))
    assert out.say.startswith("No intro times known") and seeks == [120_000] and not watched
