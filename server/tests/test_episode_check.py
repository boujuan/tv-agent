"""Whether a file is the episode it's named (episode_check.py): Netflix's rips of Community season 2 skip the pulled
"Advanced Dungeons & Dragons", so their "S02E14" is the aired 15th (found 2026-10-23)."""
import asyncio
import types

from tv_agent import episode_check as E
from tv_agent.agent import Agent
from tv_agent.state import NowPlaying
from tv_agent.stremio import streams as S

DND = [(60_000 + i * 3000, x) for i, x in enumerate([
    "It was Jeff who pretended to be interested", "in dungeons and dragons in an effort",
    "to lift Neil's spirits.", "Fat Neil is going to kill himself", "Pierce wasn't invited",
    "Hector the Well-Endowed rolls initiative", "the dwarven elders demand tribute"] * 4)]
ROMANCE = [(60_000 + i * 3000, x) for i, x in enumerate([
    "Liverpool are playing Manchester United", "I'm a stylish American, professor",
    "Britta and Page are going to the dance", "Valentine's day is a construct", "Duncan has a massive television",
    "Abed wants a sitcom ending for Troy", "the Canadian alt-rock band was celebrated"] * 4)]
IDS = ["tt1:2:13", "tt1:2:14", "tt1:2:15", "tt1:2:16"]


def test_the_same_lines_match_and_another_episode_doesnt():
    share, shift = E.compare([(t - 2000, x) for t, x in DND[:8]], DND)
    assert share > 0.9 and shift == 2.0          # the episode's subtitles run 2 s later than this file
    assert E.compare(ROMANCE[:8], DND)[0] < E.UNLIKE
    assert E.neighbours("tt1:2:14", IDS) == ["tt1:2:15", "tt1:2:13", "tt1:2:16"]


def run_check(file_lines, monkeypatch):
    async def file_cues(url, *a):
        return ("eng", file_lines) if file_lines is not None else None

    async def episode_subs(video, lang):
        return {"tt1:2:14": DND, "tt1:2:15": ROMANCE}.get(video, [(i * 1000, "nothing alike here") for i in range(40)])
    monkeypatch.setattr(E, "file_cues", file_cues)
    return asyncio.run(E.check("https://x/f.mkv", "tt1:2:14", episode_subs, IDS))


def test_a_file_named_14_that_is_15_is_found_out(monkeypatch):
    v = run_check(ROMANCE[:8], monkeypatch)
    assert v.ok is False and v.actual == "tt1:2:15"
    v = run_check(DND[:8], monkeypatch)
    assert v.ok is True and v.actual is None
    assert run_check(None, monkeypatch).ok is None              # no text subtitles in the file: can't tell


def test_what_is_found_is_kept_and_a_file_can_stand_in_for_its_real_episode(tmp_path):
    checks = E.Checks(tmp_path / "checks.json")
    checks.keep("NF.S02E14.mkv", 9, E.Verdict("tt1:2:14", False, actual="tt1:2:15"))
    again = E.Checks(tmp_path / "checks.json")
    assert again.get("NF.S02E14.mkv", 9, "tt1:2:14").ok is False and again.get("NF.S02E14.mkv", 9, "tt1:2:15") is None
    assert again.playing_as("tt1:2:15") == [("NF.S02E14.mkv", 9, "tt1:2:14")]


def agent_with(tmp_path, by_video: dict):
    a = Agent.__new__(Agent)
    a.tvplayer = types.SimpleNamespace(now=lambda: None)        # TV Agent Player not open
    a.checks = E.Checks(tmp_path / "checks.json")
    a.cfg = types.SimpleNamespace(max_height=2160, max_mbps=0, player="kodi")
    a.track_plan = lambda meta: types.SimpleNamespace(subtitles=None, audio="eng")

    async def fetch_streams(kind, video):
        return [S.Stream("AIOStreams", f"https://x/{f}", f, "", f, 0, height=int(res[:-1]), size=size, cached=True)
                for f, size, res in by_video.get(video, [])]
    a.fetch_streams = fetch_streams
    return a


def test_a_wrong_file_is_left_out_and_the_right_one_named_otherwise_is_offered(tmp_path):
    a = agent_with(tmp_path, {
        "tt1:2:15": [("Community.S02E15.2160p.NF.mkv", 2, "2160p"), ("Community.S02E15.1080p.BluRay.mkv", 3, "1080p")],
        "tt1:2:14": [("Community.S02E14.2160p.NF.mkv", 1, "2160p")]})
    a.checks.keep("Community.S02E15.2160p.NF.mkv", 2, E.Verdict("tt1:2:15", False, actual="tt1:2:16"))
    a.checks.keep("Community.S02E14.2160p.NF.mkv", 1, E.Verdict("tt1:2:14", False, actual="tt1:2:15"))
    found = asyncio.run(Agent.streams_for(a, "series", "tt1:2:15"))
    usable = [s.filename for s in found if not s.why_dropped]
    assert usable[0] == "Community.S02E14.2160p.NF.mkv"        # the 4K that is aired 15, named 14
    assert "Community.S02E15.2160p.NF.mkv" not in usable and "Community.S02E15.1080p.BluRay.mkv" in usable


def test_playing_the_wrong_file_switches_to_a_right_one_where_it_was(tmp_path):
    a = agent_with(tmp_path, {"tt1:2:14": [("Community.S02E14.2160p.NF.mkv", 1, "2160p"),
                                           ("Community.S02E14.1080p.BluRay.mkv", 2, "1080p")]})
    np = NowPlaying("tt1", "series", "tt1:2:14", "Community", "kodi")
    a.state = types.SimpleNamespace(now_playing=np)
    played, said = [], []

    async def notify(text):
        said.append(text)

    async def player():
        return 1

    async def position(pid):
        return 266_000, 1_277_000, 1

    async def play(*args, **kw):
        played.append((args[6], kw["prepared"].filename, kw["jump"]))
    a.notify, a._kodi_player, a.play = notify, player, play
    a.kodi = types.SimpleNamespace(position=position)
    wrong = S.Stream("AIOStreams", "https://x/1", "n", "", "Community.S02E14.2160p.NF.mkv", 1)

    async def run():
        verdict = E.Verdict("tt1:2:14", False, actual="tt1:2:15")
        a.checks.keep(wrong.filename, 1, verdict)
        done = asyncio.get_running_loop().create_future()
        done.set_result(verdict)
        await Agent._switch_if_wrong(a, np, wrong, done, ("tt1", "series", "Community", "tt1:2:14", "Community · S2E14",
                                                          {}))
    asyncio.run(run())
    assert played == [(266_000, "Community.S02E14.1080p.BluRay.mkv", True)]
    assert said and "another episode" in said[0]
