"""Live audio from the TV app, so the decision is made during your pause instead of after a fixed 700 ms of quiet.

The TV app streams the remote's microphone in 20 ms chunks from the moment the pill opens (protocol in server.py).
This module decides when you've finished, from the audio as it arrives (the same energy measure the app uses):

  ~160 ms of quiet  a complete command that can't be the start of a longer one, from a fixed phrase or pattern
                    ("pause", "next episode", "go home", "… season 3 episode 2")
  ~460 ms           anything else understood that doesn't end mid-phrase ("volume twenty", d1's answers)
  ~720 ms           whatever was heard (the old fixed wait), or at once when the app ends the audio. A title or a
                    search on its own waits this long too: people pause before the season ("play the bear … season 3")

At each step the audio so far is transcribed and decided; if you start talking again, that result is dropped.
"""
import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

FRAME = 320                          # samples: 20 ms at 16 kHz
START_FRAMES = 3                     # 60 ms above the threshold = speech
PREROLL = 15                         # frames kept before speech starts (300 ms), as the app does
CHECKPOINTS = (8, 23, 36)            # frames of quiet after speech: 160, 460, 720 ms
# Last words that mean more is coming ("play the …", "season …", "volume …", "skip thirty …").
OPEN_ENDED = re.compile(r"\b(?:the|a|an|to|of|on|in|and|with|by|for|from|at|season|series|episode|ep|part|play|watch|"
                        r"put|open|go|start|continue|resume|volume|set|skip|jump|seek|search|turn|switch)$")
# A number that can still grow: "twenty" → "twenty five", "30" → "35". "two" or "fifteen" is final.
TRAILING_NUMBER = re.compile(r"(?:\b\d*0|\b(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred))$")
# Whole commands that are often the first word of a longer one ("play" → "play the bear").
PREFIXES = {"play", "continue", "resume", "start", "watch", "open", "put", "go", "skip", "keep", "turn", "set",
            "jump", "next", "go back", "back"}


def level_db(frame: np.ndarray) -> float:
    rms = math.sqrt(float(np.mean(frame.astype(np.float64) ** 2))) / 32768.0 if len(frame) else 0.0
    return 20 * math.log10(rms) if rms > 0 else -120.0


@dataclass
class Endpointer:
    """Speech start and the quiet after it, frame by frame (the app's rule: threshold from the first 200 ms that
    carry sound, 12 dB above their background, clamped to -55…-30 dB)."""
    threshold: float = -45.0
    speaking: bool = False
    quiet: int = 0                   # frames of quiet since the last voiced frame
    voiced: int = 0                  # voiced frames so far: changes whenever you're talking
    last_voice_at: float = 0.0       # monotonic time of the last voiced frame
    _early: list = field(default_factory=list)
    _loud: int = 0

    def feed(self, frame: np.ndarray, now: float | None = None):
        level = level_db(frame)
        if len(self._early) < 10 and level > -110:
            self._early.append(level)
            if len(self._early) == 10:
                self.threshold = max(-55.0, min(-30.0, sorted(self._early)[2] + 12))
        self._loud = self._loud + 1 if level > self.threshold else 0
        if not self.speaking:
            if self._loud >= START_FRAMES:
                self.speaking = True
                self.voiced += 1
                self.last_voice_at = now if now is not None else time.monotonic()
            return
        if level < self.threshold - 3:
            self.quiet += 1
        else:
            self.quiet = 0
            self.voiced += 1
            self.last_voice_at = now if now is not None else time.monotonic()

    @property
    def stage(self) -> int:
        """-1 while talking (or before), else how many checkpoints the quiet has passed (0, 1, 2)."""
        if not self.speaking:
            return -1
        return sum(self.quiet >= c for c in CHECKPOINTS) - 1


def ready(action, via: str, text: str, stage: int) -> bool:
    """Whether a decision made after `stage` checkpoints of quiet should be acted on now. text is the cleaned-up
    sentence (router.normalize)."""
    if stage >= 2:
        return True
    if action.type == "none" or text in PREFIXES:
        return False
    if action.type != "next_episode" and OPEN_ENDED.search(text):   # "next episode" is whole; "season 3 episode" isn't
        return False
    if action.type in ("youtube", "twitch") or (action.type == "play_title" and action.episode is None):
        return False                 # a title or search on its own is often followed by more ("… season 3")
    if stage == 1:
        return True
    if via not in ("phrase", "pattern") or TRAILING_NUMBER.search(text):
        return False
    if action.type in ("volume", "home", "next_episode", "open_in_stremio", "open_app", "transport", "show_list",
                       "pick", "library", "skip", "auto_skip", "wrong"):
        return True
    return action.type == "play_title"   # with an episode (title-only waited above); continue_watching waits


class StreamSession:
    """Audio in, one decision out. transcribe(bytes) -> text, decide(text) -> Decision, normalize(text) -> text and
    send(dict) are async/sync callables supplied by the server (and by tests)."""

    def __init__(self, transcribe, decide, normalize, send):
        self.transcribe, self.decide, self.normalize, self.send = transcribe, decide, normalize, send
        self.pcm = bytearray()
        self.ep = Endpointer()
        self._pos = 0                # bytes of pcm already measured
        self._from = 0               # where the audio worth transcribing starts (just before speech)
        self._stage = -1             # highest checkpoint already checked since you last spoke
        self._lock = asyncio.Lock()
        self._cache = None           # (voiced, text, decision): the audio's meaning doesn't change in quiet
        self._tasks = []
        self.committed = asyncio.Event()
        self.result = None           # (decision, text)
        self.endpoint_ms = None      # from your last word to the decision

    def feed(self, data: bytes, now: float | None = None):
        self.pcm += data
        while len(self.pcm) - self._pos >= FRAME * 2:
            frame = np.frombuffer(bytes(self.pcm[self._pos:self._pos + FRAME * 2]), dtype="<i2")
            self._pos += FRAME * 2
            before, was_speaking = self.ep.voiced, self.ep.speaking
            self.ep.feed(frame, now)
            if self.ep.speaking and not was_speaking:
                self._from = max(0, self._pos - (START_FRAMES + PREROLL) * FRAME * 2)
            if self.ep.voiced != before:
                self._stage = -1     # talking again: earlier checkpoints count afresh
            stage = self.ep.stage
            if stage > self._stage and not self.committed.is_set():
                self._stage = stage
                self._tasks.append(asyncio.ensure_future(self._check(stage)))

    async def _check(self, stage: int):
        try:
            await self._decide(stage)
        except asyncio.CancelledError:
            raise
        except Exception as e:      # a failed transcription or a closed connection: the next checkpoint retries
            log.warning("deciding at checkpoint %d: %s", stage, type(e).__name__)

    async def _decide(self, stage: int):
        async with self._lock:
            if self.committed.is_set():
                return
            voiced = self.ep.voiced
            if self._cache and self._cache[0] == voiced:
                text, d = self._cache[1], self._cache[2]
            else:
                text = await self.transcribe(bytes(self.pcm[self._from:]))
                if self.ep.voiced != voiced and stage < 2:
                    return           # you went on talking while it was being transcribed
                await self._send({"heard": text})
                d = await self.decide(text)
                self._cache = (voiced, text, d)
            if self.ep.voiced != voiced and stage < 2:
                return
            if ready(d.action, d.via, self.normalize(text), stage):
                self._commit(d, text)

    def _commit(self, d, text: str):
        if self.committed.is_set():
            return
        self.result = (d, text)
        if self.ep.speaking:
            self.endpoint_ms = (time.monotonic() - self.ep.last_voice_at) * 1000
        self.committed.set()

    async def _send(self, msg: dict):
        r = self.send(msg)
        if asyncio.iscoroutine(r):
            await r

    async def finish(self):
        """The audio ended (or broke off): decide on what there is, unless that's already done."""
        if not self.committed.is_set():
            await self._check(2)
        for t in self._tasks:
            if not t.done():
                t.cancel()
        return self.result
