"""Preview pictures for a held ←/→ (tv/thumbs.py): a Matroska file's index read once, one keyframe per picture, and
the player's `thumb_req` answered with `thumb`. The files are made here with ffmpeg and read from memory; nothing is
fetched from the network."""
import asyncio
import base64
import shutil
import subprocess

import pytest

from tv_agent.tv import thumbs as TH
from tv_agent.tv import tvplayer as T

from test_tvplayer import server, tls  # noqa: F401  (tls is a fixture)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture(scope="module")
def mkv(tmp_path_factory) -> bytes:
    """20 s of a test pattern, a keyframe every 2 s, with an audio track interleaved (as films have)."""
    out = tmp_path_factory.mktemp("thumbs") / "f.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=20",
                    "-f", "lavfi", "-i", "sine=d=20", "-c:v", "libx264", "-g", "48", "-keyint_min", "48",
                    "-sc_threshold", "0", "-c:a", "aac", str(out)], check=True)
    return out.read_bytes()


def reader(data: bytes, reads: list | None = None):
    async def fetch(start: int, length: int) -> bytes:
        if reads is not None:
            reads.append((start, length))
        return data[start:start + length]
    return fetch


def frames(one: bytes) -> int:
    p = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", "-f", "matroska", "-i", "pipe:0"], input=one,
                       capture_output=True, check=True)
    return int(p.stdout.strip())


def test_the_index_is_read_once_and_each_picture_is_one_keyframe(mkv):
    async def run():
        reads = []
        m = await TH.Mkv.read(reader(mkv, reads))
        assert len(reads) == 2                              # the head, then the Cues at the end
        assert m.codec == "V_MPEG4/ISO/AVC" and not m.dv and m.track == 1
        assert m.times == list(range(0, 20_000, 2_000))
        assert m.cue(5_000) == 2 and m.cue(4_000) == 2 and m.cue(0) == 0 and m.cue(99_000) == 9
        reads.clear()
        one = await m.keyframe(reader(mkv, reads), m.cue(13_500))
        assert len(reads) == 1 and frames(one) == 1        # one range read; a file with that frame only
        pic = await TH.jpeg(one, TH.chain(m, 160))
        assert pic[:2] == b"\xff\xd8"
    asyncio.run(run())


def test_a_keyframe_is_found_without_its_place_in_the_cluster_and_across_short_reads(mkv, monkeypatch):
    async def run():
        m = await TH.Mkv.read(reader(mkv))
        m.places = [(c, None, t) for c, _, t in m.places]  # an index without CueRelativePosition (older muxers)
        monkeypatch.setattr(TH, "BLOCK", 100)               # a first read far shorter than the keyframe
        reads = []
        one = await m.keyframe(reader(mkv, reads), 7)
        assert len(reads) > 1 and frames(one) == 1
        assert (await TH.jpeg(one, TH.chain(m, 160)))[:2] == b"\xff\xd8"
    asyncio.run(run())


def test_only_matroska_with_an_index(mkv, tmp_path):
    mp4 = tmp_path / "f.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:d=2",
                    "-c:v", "libx264", str(mp4)], check=True)
    with pytest.raises(TH.ThumbError, match="not a Matroska file"):
        asyncio.run(TH.Mkv.read(reader(mp4.read_bytes())))
    i = mkv.find(b"\x1c\x53\xbb\x6b")                       # the Cues' ID, renamed: no index
    with pytest.raises(TH.ThumbError, match="no seek index"):
        asyncio.run(TH.Mkv.read(reader(mkv[:i] + b"\xec" + mkv[i + 1:])))


def test_hdr_and_dolby_vision_are_tone_mapped():
    m = TH.Mkv()
    m.codec = "V_MPEG4/ISO/AVC"
    assert TH.chain(m, 320).startswith("scale=320:-2")
    m.codec, m.transfer = "V_MPEGH/ISO/HEVC", 16
    assert TH.chain(m, 320).startswith("libplacebo=w=320") and "zscale" in TH.chain(m, 320, placebo=False)
    m.transfer, m.dv = 0, True
    assert "libplacebo" in TH.chain(m, 320) and "tonemap" in TH.chain(m, 320, placebo=False)
    m.dv = False                                            # HEVC that doesn't say: libplacebo reads the frame's
    assert "libplacebo" in TH.chain(m, 320) and TH.chain(m, 320, placebo=False).startswith("scale")


def test_the_player_asks_and_gets_a_picture_of_the_file_playing(tls, mkv, monkeypatch):  # noqa: F811
    async def run():
        p, srv, fake = await server(tls)
        await fake.connect()
        await asyncio.gather(fake.answer(), p.open("https://debrid/f.mkv", 0, item="tt1", title="X"))
        item = p.item

        async def fetch(start: int, length: int) -> bytes:
            assert p.thumbs._link == ("https://debrid/f.mkv", {})
            return mkv[start:start + length]
        monkeypatch.setattr(p.thumbs, "_fetch", fetch)
        await fake.send({"t": "thumb_req", "item": item, "ms": 9_000, "w": 160})
        cmd = await fake.answer()
        assert cmd["t"] == "thumb" and cmd["item"] == item and cmd["ms"] == 9_000 and cmd["at"] == 8_000
        assert base64.b64decode(cmd["jpeg"])[:2] == b"\xff\xd8"
        await fake.send({"t": "thumb_req", "item": "tt0#old", "ms": 9_000, "w": 160})
        cmd = await fake.answer()
        assert cmd["t"] == "thumb" and cmd["error"] == "not the file playing" and "jpeg" not in cmd
        assert "thumb_req" not in p.last
        srv.close()
    asyncio.run(run())


def test_a_request_overtaken_while_it_waited_is_dropped(mkv, monkeypatch):
    async def run():
        th = TH.Thumbs()
        th.follow("a#1", "https://debrid/f.mkv", None)
        gate = asyncio.Event()

        async def fetch(start: int, length: int) -> bytes:
            await gate.wait()
            return mkv[start:start + length]
        monkeypatch.setattr(th, "_fetch", fetch)
        first = asyncio.create_task(th.picture("a#1", 2_000, 160))
        await asyncio.sleep(0.01)
        waiting = asyncio.create_task(th.picture("a#1", 4_000, 160))
        await asyncio.sleep(0.01)
        latest = asyncio.create_task(th.picture("a#1", 6_000, 160))
        gate.set()
        a, b, c = await asyncio.gather(first, waiting, latest)
        assert a[0] == 2_000 and b is None and c[0] == 6_000
        assert (await th.picture("a#1", 2_500, 160))[1] == a[1]        # the same keyframe: kept
        assert len(th.made) == 2
    asyncio.run(run())


def test_the_index_is_read_soon_after_the_file_opens(mkv, monkeypatch):
    async def run():
        monkeypatch.setattr(TH, "INDEX_AFTER_S", 0.01)
        th = TH.Thumbs()
        reads = []

        async def fetch(start: int, length: int) -> bytes:
            reads.append(start)
            return mkv[start:start + length]
        monkeypatch.setattr(th, "_fetch", fetch)
        th.follow("a#1", "https://debrid/f.mkv", None)
        await asyncio.sleep(0.1)
        assert len(reads) == 2                              # the head and the Cues, before any request
        reads.clear()
        at, _ = await th.picture("a#1", 9_000, 160)
        assert at == 8_000 and len(reads) == 1              # then only the keyframe
        th.follow("b#2", "https://debrid/g.mkv", None)      # another file: its own, later
        th.follow("c#3", "https://debrid/h.mkv", None)
        await asyncio.sleep(0.1)
        assert th._mkv is not None and len(reads) == 3      # b#2's was cancelled before it read anything
    asyncio.run(run())


def test_the_ranges_a_start_reads_are_the_index_and_the_cluster_of_its_keyframe(mkv):
    """TV Agent Player's `prefetch`: asked for at once instead of the header, then the Cues, then the start."""
    m = asyncio.run(TH.Mkv.read(reader(mkv)))
    at, n = m.cues_at
    assert mkv[at:at + 4] == b"\x1c\x53\xbb\x6b" and TH.element(mkv, at)[2] == at + n       # the whole Cues
    cues, start = m.ranges(9_000)
    assert cues == {"at": at, "bytes": n} and start["bytes"] == TH.START_AHEAD
    assert mkv[start["at"]:start["at"] + 4] == b"\x1f\x43\xb6\x75"                        # a cluster
    assert start["at"] == m.segment + m.places[m.cue(9_000)][0] and m.times[m.cue(9_000)] == 8_000
    assert m.ranges(0)[1]["at"] < start["at"]


def test_a_files_index_is_kept_for_its_next_start_and_its_previews(mkv, monkeypatch):
    async def run():
        monkeypatch.setattr(TH, "INDEX_AFTER_S", 0.01)
        monkeypatch.setattr(TH, "_layouts", TH.OrderedDict())
        th, reads = TH.Thumbs(), []

        async def fetch(start: int, length: int) -> bytes:
            reads.append(start)
            return mkv[start:start + length]
        monkeypatch.setattr(th, "_fetch", fetch)
        th.follow("a#1", "https://debrid/f.mkv", None, ("f.mkv", len(mkv)))
        await asyncio.sleep(0.1)
        assert len(reads) == 2 and TH.layout(("f.mkv", len(mkv))) is not None
        th.follow("a#2", "https://debrid/other-link-same-file", None, ("f.mkv", len(mkv)))   # played again
        await asyncio.sleep(0.1)
        assert len(reads) == 2                              # nothing read again
        th.follow("b#3", "https://debrid/g.mkv", None)      # a file without a name: read, not kept
        await asyncio.sleep(0.1)
        assert len(reads) == 4 and len(TH._layouts) == 1
    asyncio.run(run())
