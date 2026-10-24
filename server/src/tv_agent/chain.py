"""Chains, run: an actions.Sequence's steps one after the other ("play The Bear S2E5, then go to 12:00"; reading
one: decide/chain.py). Each step starts only once the one before has done what it said:

1. The plan first: on the TV ("The Bear S2E5, then 12:00 · “cancel” or Back stops it") before anything starts, and
   on TV Agent Player's loading screen while the title starts (PLAN).
2. The play step, then its proof: something new playing, the episode asked for (by its id), its picture moving
   (TV Agent Player's first frame, Kodi's length, Just Player's state), within PLAY_WAIT.
3. Each step after it, while the same title still plays: done, then checked within its own limit (WAIT): the
   position near the time, the track in the language, the volume, the pause. A language change or a pause first
   waits for the episode check's verdict on a file it hasn't checked before (VERDICT_WAIT): a wrong file is
   replaced by the right one at the same moment, which keeps a jump but not a track or a pause.
4. A step that fails or runs out of time stops the chain, saying which step and why. Nothing is tried again.
5. "Cancel", any other command (but the volume), or Back in TV Agent Player stops it at once. A start still on its
   way stops too, its loading screen closed; a title already playing goes on.
"""
import asyncio
import contextlib
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from . import actions as A
from .languages import matches, name as language_name
from .tv import media, tvplayer

log = logging.getLogger(__name__)
PLAN: ContextVar[str] = ContextVar("plan", default="")   # what comes after the play, said while it starts
# The steps after the play, while it starts: a list shown instead (which episode?) keeps them for the pick
REST: ContextVar[tuple] = ContextVar("rest", default=())
# Whether the command being run replaced one still on its way (server.Service.start): "cancel" then has a start to stop
INTERRUPTED: ContextVar[bool] = ContextVar("interrupted", default=False)
PLAY_WAIT = 75.0            # the play step: the title found, its streams, the player's first frame
# Each step after the play, done and checked. A language the file lacks switches to another copy (agent._tracks).
WAIT = {"jump": 20.0, "find_scene": 50.0, "skip": 15.0, "tracks": 30.0, "volume": 8.0, "pause": 8.0}
VERDICT_WAIT = 25.0         # the episode check of a file not checked before (~10 s from the start)
NEAR_MS = 10_000            # a jump is where it was asked to go when the position is this close
KODI_SETTLES = 1.5          # Kodi jumps to where you stopped by itself right after it starts
STOPS = "“cancel” or Back stops it"
SCENE_SHOWN = 2.5           # the line a scene was found by stays on the TV this long before the jump
SCENE_BEFORE = 3            # the jump lands this many seconds before that line


class Stopped(Exception):
    """The chain was stopped: "cancel", or Back in the player."""


@dataclass
class Run:
    steps: list
    labels: list[str]
    at: int = 0                                   # the step running, from 1
    why: str = ""                                 # why it was stopped
    playing: object = None                        # the NowPlaying the play step started
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    ended: float = 0.0                            # monotonic time it ended (for "cancel" just after)

    def cancel(self, why: str):
        if not self.stop.is_set():
            self.why = why
            self.stop.set()

    def left(self) -> str:
        return ", ".join(self.labels[max(self.at - 1, 0):]) or "nothing"

    async def within(self, coro, seconds: float):
        """coro's result, unless the chain is stopped first (Stopped) or it takes longer (TimeoutError). Either
        way, or if the chain itself is cancelled, coro is cancelled."""
        task = asyncio.ensure_future(coro)
        stopped = asyncio.ensure_future(self.stop.wait())
        try:
            done, _ = await asyncio.wait({task, stopped}, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            task.cancel()
            raise
        finally:
            stopped.cancel()
        if task in done:
            return task.result()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        if self.stop.is_set():
            raise Stopped(self.why)
        raise TimeoutError


def label(step) -> str:
    """A step as the plan says it: "The Bear S2E5", "12:00", "the scene: “Paul rides the worm”"."""
    match step:
        case A.PlayTitle():
            ep = (f"S{step.season}" if step.season is not None else "") + \
                 (f"E{step.episode}" if step.episode is not None else "")
            what = step.title.title() if step.title else "this show"
            if step.latest:
                return f"the latest {what}"
            if step.episode_name:
                return f"{what}: “{step.episode_name}”"
            return f"{what} {ep}".strip() + (" from the start" if step.restart else "")
        case A.ContinueWatching():
            return f"{step.title.title()} where you were" if step.title else "what you were watching"
        case A.NextEpisode():
            return "the next episode" if step.offset > 0 else "the previous episode"
        case A.Transport(op="seek_to") if step.percent is not None:
            return "the middle" if step.percent == 50 else f"{step.percent}%"
        case A.Transport(op="seek_to"):
            return "the start" if not step.seconds else clock(step.seconds * 1000)
        case A.Transport(op="pause"):
            return "pause"
        case A.FindScene():
            return f"the scene: “{step.what}”"
        case A.Skip():
            return f"skip the {step.what}"
        case A.Tracks():
            parts = []
            if step.audio:
                parts.append("original audio" if step.audio == "original" else f"{language_name(step.audio)} audio")
            if step.subtitles:
                parts.append({"on": "subtitles on", "off": "subtitles off", "toggle": "subtitles"}.get(step.subtitles)
                             or f"{language_name(step.subtitles)} subtitles")
            return ", ".join(parts)
        case A.Volume():
            return {"mute": "mute", "unmute": "sound on", "set": f"volume {step.amount}"}.get(step.op, f"volume {step.op}")
    return step.type.replace("_", " ")


def plan(a: A.Sequence) -> str:
    """"The Bear S2E5, then 12:00, then Spanish subtitles"."""
    labels = [label(s) for s in a.steps]
    return ", then ".join([labels[0][:1].upper() + labels[0][1:], *labels[1:]])


def clock(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


async def run(agent, a: A.Sequence, heard: str = ""):
    """The chain, step by step; (ok, what to say, details for the history). `heard`: the sentence as transcribed (the
    loading screen quotes it; a scene search reads it for words cut off)."""
    r = Run(list(a.steps), [label(s) for s in a.steps])
    agent.chain = r
    seen_up = [agent.tvplayer.up]

    def on_event(msg: dict):                  # Back (or Home) in TV Agent Player, on what the chain plays
        if msg.get("t") == "closed" and msg.get("why") in ("back", "home") and (
                r.playing is None or msg.get("item") == agent.tvplayer.item):
            r.cancel("Back was pressed")

    async def watch():                        # its screen closed while the title starts
        while True:
            await asyncio.sleep(0.25)
            if agent.tvplayer.up:
                seen_up[0] = True
            elif seen_up[0] and agent.cfg.player == "tvplayer":
                r.cancel("the player was closed")
                return

    agent.tvplayer.listeners.append(on_event)
    watcher = asyncio.create_task(watch())
    details: dict = {"chain": r.labels}
    try:
        await agent.notify(f"{plan(a)} · {STOPS}")
        return await _steps(agent, r, details, heard)
    except Stopped as e:
        log.info("chain stopped at step %d: %s", r.at, e)
        if r.playing is None:
            await abandon_start(agent)
        return False, f"Stopped: {e} · not done: {r.left()}", details
    except asyncio.CancelledError:
        r.why = r.why or "another command came"
        log.info("chain cancelled at step %d", r.at)
        raise
    finally:
        r.ended = time.monotonic()
        r.finished.set()
        watcher.cancel()
        with contextlib.suppress(ValueError):
            agent.tvplayer.listeners.remove(on_event)


async def _steps(agent, r: Run, details: dict, heard: str):
    first, rest = r.steps[0], r.steps[1:]
    r.at = 1
    before = agent.state.now_playing
    PLAN.set(", then ".join(r.labels[1:]))
    REST.set(tuple(rest))
    t0, why = time.monotonic(), None
    try:
        out = await r.within(agent.execute(first, heard), PLAY_WAIT)
        if out.ok:
            why = await r.within(_playing(agent, first, before), max(5.0, PLAY_WAIT - (time.monotonic() - t0)))
    except TimeoutError:
        await abandon_start(agent)
        return False, f"Step 1 ({r.labels[0]}) didn't start within {PLAY_WAIT:.0f} s · not done: {r.left()}", details
    finally:
        PLAN.set("")
        REST.set(())
    details.update({k: v for k, v in (out.details or {}).items() if k not in details})
    if not out.ok:
        return False, f"Step 1 ({r.labels[0]}): {out.say} · not done: {', '.join(r.labels[1:])}", details
    if why:
        return False, f"Step 1 ({r.labels[0]}): {why} · stopped before {', '.join(r.labels[1:])}", details
    r.playing = agent.state.now_playing
    said = [out.say]
    for n, step in enumerate(rest, 2):
        r.at = n
        kind, later = _limit(step), ", ".join(r.labels[n:]) or "nothing"
        try:
            if why := await _still(agent, r):
                return False, f"Step {n} ({r.labels[n - 1]}): {why} · not done: {r.left()}", details
            if kind in ("tracks", "pause"):
                try:
                    await r.within(_verdict(agent, r), VERDICT_WAIT + 10)
                except TimeoutError:             # the step goes on; its own check says if nothing plays
                    log.info("chain: no episode check verdict in time")
            why, done = await r.within(_follow(agent, step, heard), WAIT[kind])
        except TimeoutError:
            return False, f"Step {n} ({r.labels[n - 1]}) didn't finish within {WAIT[kind]:.0f} s · not done: " \
                          f"{later}", details
        except (Stopped, asyncio.CancelledError):
            raise
        except Exception as e:
            log.exception("chain step %d", n)
            return False, f"Step {n} ({r.labels[n - 1]}): it failed ({type(e).__name__}) · not done: {later}", details
        if why:
            return False, f"Step {n} ({r.labels[n - 1]}): {why} · not done: {later}", details
        said.append(done)
    r.at = len(r.steps) + 1
    return True, " · ".join(said), details


def _limit(step) -> str:
    """Which of WAIT a step after the play gets."""
    if step.type in ("find_scene", "skip"):
        return step.type
    kind = A.follow_kinds(step)[0]
    return "tracks" if kind in ("audio", "subtitles") else kind


def _plays(player) -> bool:
    """Whether TV Agent Player has a file playing: it has said where it is (an item it was sent but never loaded, a
    start the player closed on, says nothing)."""
    return player.now() is not None and "pos" in player.last


def _loading(player) -> bool:
    """Whether TV Agent Player shows a loading screen for a start that hasn't loaded."""
    return player.up and getattr(player, "_coming", None) is not None


async def abandon_start(agent, loading_only: bool = False):
    """A start that was stopped: TV Agent Player's loading screen taken away (the player closed, unless something
    plays behind it), or, with no player screen up and not `loading_only`, back to Home unless something plays."""
    p = agent.tvplayer
    try:
        if p.up and (not loading_only or _loading(p)):
            if _plays(p):
                await p.clear_loading()
            else:
                await p.stop(tvplayer.PID)
        elif not loading_only:
            await agent._home_unless_playing()
    except Exception as e:
        log.info("closing a stopped start: %s", type(e).__name__)


async def cancel(agent) -> tuple[bool, str]:
    """"Cancel": the chain running (or one stopped by this very command a moment ago) is stopped, and a start on
    its way with it; what was left is said."""
    r = getattr(agent, "chain", None)
    if r is None or (r.finished.is_set() and time.monotonic() - r.ended > 3):
        if not INTERRUPTED.get() and not _loading(agent.tvplayer):
            return False, "Nothing to cancel"
        await abandon_start(agent, loading_only=True)   # a start this command replaced (server.start cancels it)
        return True, "Cancelled"
    r.cancel("you said cancel")
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(r.finished.wait(), 5)
    if r.playing is None:
        await abandon_start(agent)
    return True, f"Cancelled · not done: {r.left()}"


# ---- the proof of each step ----

def _episode(step) -> str | None:
    """The end of the video id the play step must start: ":2:5" for S2E5."""
    if isinstance(step, A.PlayTitle) and step.episode is not None:
        return (f":{step.season}" if step.season is not None else "") + f":{step.episode}"
    return None


async def _playing(agent, step, before) -> str | None:
    """None once the play step's title plays (something new, the episode asked for, the picture moving), else why
    not. Polled until then: the caller sets the limit."""
    while True:
        np = agent.state.now_playing
        if np is not None and np is not before:
            if (want := _episode(step)) and not np.video_id.endswith(want):
                got = re.match(r"^tt\d+:(\d+):(\d+)$", np.video_id)
                return f"it started {f'S{got[1]}E{got[2]}' if got else np.video_id} instead"
            if await _moving(agent, np):
                return None
        await asyncio.sleep(0.4)


async def _moving(agent, np) -> bool:
    try:
        if np.player == "tvplayer":
            now = agent.tvplayer.now()
            return now is not None and (bool(agent.tvplayer.last.get("first_frame")) or (now[0] > 0 and now[2] > 0))
        if np.player == "kodi":
            pid = await agent._kodi_player()
            if pid is not None and (await agent.kodi.position(pid))[1] > 0:
                await asyncio.sleep(KODI_SETTLES)
                return True
            return False
        st = await agent._just_status()
        return bool(st and st[0] == "PLAYING")
    except Exception:
        return False


async def _still(agent, r: Run) -> str | None:
    """None while the chain's title still plays, else why not."""
    np = agent.state.now_playing
    if np is None or np.video_id != r.playing.video_id:
        return "something else plays now"
    if await _position(agent) is None:
        return "nothing plays any more"
    return None


async def _verdict(agent, r: Run):
    """The episode check's verdict on this file, if it's still being made; a file it replaced must play again."""
    task = agent._verify_task
    if task is not None and not task.done():
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(task), VERDICT_WAIT)
    np = agent.state.now_playing
    if np is not None and np is not r.playing and np.video_id == r.playing.video_id:
        r.playing = np                          # the right file, opened at the same moment
        while not await _moving(agent, np):
            await asyncio.sleep(0.4)


async def _position(agent) -> tuple[int, int, float] | None:
    """(position ms, length ms, speed) in the player, or None when nothing plays."""
    try:
        if (pid := await agent._player_id()) is not None:
            return await agent._ctl(pid).position(pid)
        if (st := await agent._just_status()) is not None:
            np = agent.state.now_playing
            return st[1], (np.runtime_ms if np else 0) or 0, 1.0 if st[0] == "PLAYING" else 0.0
    except Exception as e:
        log.info("position: %s", type(e).__name__)
    return None


async def _near(agent, target_ms: int):
    while True:
        now = await _position(agent)
        if now is not None and abs(now[0] - target_ms) <= NEAR_MS:
            return
        await asyncio.sleep(0.3)


async def _follow(agent, step, heard: str = "") -> tuple[str | None, str]:
    """A step after the play, done and checked: (None, what it did) or (why it failed, "")."""
    match step:
        case A.Transport(op="seek_to"):
            now = await _position(agent)
            if now is None:
                return "nothing plays to jump in", ""
            total = now[1]
            target = (total * step.percent // 100) if step.percent is not None else (step.seconds or 0) * 1000
            if step.percent is not None and not total:
                return "its length isn't known", ""
            if total and target >= total - 5_000:
                return f"{clock(target)} is past the end ({clock(total)})", ""
            out = await agent.execute(step)
            if not out.ok:
                return out.say, ""
            await _near(agent, target)
            return None, out.say
        case A.FindScene():
            out = await agent.execute(step, heard)
            if not out.ok:
                return out.say, ""
            if (at := (out.details or {}).get("scene_ms")) is not None:
                await _near(agent, at)
            return None, out.say
        case A.Skip():
            out = await agent._skip_when_it_comes(step.what)
            return (None, out.say) if out.ok else (out.say, "")
        case A.Tracks():
            if agent.state.now_playing and agent.state.now_playing.player == "just":
                return "Just Player changes languages only at the next start", ""
            out = await agent.execute(step)
            if not out.ok:
                return out.say, ""
            while why := await _tracks_differ(agent, step):
                log.info("tracks not yet as asked: %s", why)
                await asyncio.sleep(0.5)
            return None, out.say
        case A.Volume():
            out = await agent.execute(step)
            if not out.ok:
                return out.say, ""
            want = 0 if step.op == "mute" else step.amount if step.op == "set" else None
            if want is not None:
                while (level := await _volume(agent)) != want:
                    log.info("volume %s, not yet %s", level, want)
                    await asyncio.sleep(0.5)
            return None, out.say
        case A.Transport(op="pause"):
            out = await agent.execute(step)
            if not out.ok:
                return out.say, ""
            while (now := await _position(agent)) is not None and now[2] > 0:
                await asyncio.sleep(0.3)
            return (None, out.say) if now is not None else ("nothing plays any more", "")
    return f"{step.type} can't follow a play", ""


async def _volume(agent) -> int | None:
    try:
        return await agent.tv(media.get_volume)
    except Exception as e:
        log.info("volume: %s", type(e).__name__)
        return None


async def _tracks_differ(agent, step: A.Tracks) -> str | None:
    """None once the player's tracks are as the step asked, else what's still different (another copy that has the
    language may still be opening)."""
    try:
        pid = await agent._player_id()
        if pid is None:
            return "nothing plays"
        info = await agent._ctl(pid).tracks(pid)
    except Exception as e:
        return f"no tracks yet ({type(e).__name__})"
    audio, sub = info.get("currentaudiostream") or {}, info.get("currentsubtitle") or {}
    if step.audio and step.audio != "original" and not matches(audio, step.audio):
        return f"the audio is {language_name(audio.get('language')) or 'another language'}"
    if step.subtitles == "off" and info.get("subtitleenabled"):
        return "subtitles still on"
    if step.subtitles == "on" and not info.get("subtitleenabled"):
        return "subtitles still off"
    if step.subtitles not in (None, "on", "off", "toggle") and not (
            info.get("subtitleenabled") and (matches(sub, step.subtitles) or "external" in str(sub.get("name")))):
        return "the subtitles aren't in that language yet"
    return None


# ---- a scene, found in the subtitles (agent._find_scene) ----

def _plain(text: str) -> str:
    text = re.sub(r"<[^>]+>|\{[^}]*\}", " ", text.lower())          # subtitle tags
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


# A description that ends like this was cut off while it was said ("… where Paul rides the")
_DANGLING = re.compile(r"\b(?:the|a|an|with|to|of|and|or|in|on|at|for|from|by|into|where|when|his|her|their|its|my|"
                       r"your|el|la|los|las|un|una|unos|unas|con|de|del|al|y|o|en|que|donde|cuando|su|sus|por|para|"
                       r"se|le|les|mi|tu)$")


def cut_off(text: str) -> bool:
    """Whether a description (or the sentence it came in) stops mid-phrase: what it meant isn't known."""
    return bool(_DANGLING.search(_plain(text)))


def _ms_of(at: str) -> int | None:
    m = re.fullmatch(r"\s*(?:(\d+):)?(\d{1,2}):(\d\d)\s*", at or "")
    return None if not m else (int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])) * 1000


def _likeness(said: str, text: str) -> float:
    """How well a quoted line matches a cue (or two cues together): most of it, near enough word for word."""
    short, long_ = sorted((said, text), key=len)
    if len(short) < 6 or len(short) < 0.45 * len(long_):
        return 0.0
    return fuzz.partial_ratio(short, long_)


def place_scene(at: str, line: str, how: str, cues: list[tuple[int, str]]) -> tuple[int | None, str]:
    """Where a moment the LLM placed is: the time of the subtitle line it quoted, found here (a model's own arithmetic
    was hours off), or None and why not: a guess from the story alone, a line that isn't in the subtitles, one said
    in more than one place with no time to tell them apart."""
    if how not in ("words", "around"):
        return None, "the subtitles don't show it" if how == "guess" else "no reason given"
    said = _plain(line)
    if len(said) < 6:
        return None, "no line to place it by"
    plain = [_plain(w) for _, w in cues]
    # each cue, or it and the next together (a line split over two), a whole cue first
    scores = [max(_likeness(said, plain[i]), _likeness(said, f"{plain[i]} {plain[i + 1]}") - 1 if i + 1 < len(cues)
                  else 0.0) for i in range(len(cues))]
    best = max(scores, default=0.0)
    if best < 88:
        return None, "the line it gave isn't in the subtitles"
    found = [(cues[i][0], score) for i, score in enumerate(scores) if score >= best - 2]   # the best, any as good
    listed = _ms_of(at)
    if listed is not None and (near := [x for x in found if abs(x[0] - listed) <= 5_000]):
        return max(near, key=lambda x: (x[1], -abs(x[0] - listed)))[0], ""
    if found[-1][0] - found[0][0] <= 60_000:      # one place (a line said twice in a row)
        return max(found, key=lambda x: x[1])[0], ""
    return None, "that line is said in more than one place"
