"""The laptop's microphone as a second remote: record one command with PipeWire (`pw-record`, the default input),
stop when you stop talking (the TV app's end-of-speech rule, from its 0.9.2), send it to the agent, return its
answer."""
import array
import math
import subprocess
import threading
from collections import deque

from PySide6.QtCore import QObject, Signal

from .common import ApiError, api

RATE = 16000
FRAME_BYTES = RATE // 50 * 2           # 20 ms of 16-bit mono
PREROLL, START, END, GIVE_UP, MAX = 15, 3, 35, 200, 750   # frames, as in the TV app: up to 15 s
# Once you've spoken for a second, a frame within 8 dB of the quietest of the last second (the room, the TV) is quiet
# too, unless it's within 25 dB of your loudest: the threshold set from the first 200 ms can sit under the sound
# around you once it comes up (the agent's listen_stream.py rule)
BACKGROUND, ABOVE_BACKGROUND, BELOW_VOICE = 50, 8.0, 25.0


def level_db(frame: bytes) -> float:
    samples = array.array("h", frame)
    if not samples:
        return -120.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
    return 20 * math.log10(rms) if rms > 0 else -120.0


class Endpointer:
    """When one command starts and ends, frame by frame (levels in dB): feed() says "wait", "start" (speech began:
    send the frames kept before it too), "end" (you stopped talking) or "give up" (nothing said)."""

    def __init__(self):
        self.frames = self.loud = self.quiet = 0
        self.threshold, self.peak, self.speaking = -45.0, -120.0, False
        self.early: list[float] = []
        self.recent: deque[float] = deque(maxlen=BACKGROUND)      # the last second since speech began

    def feed(self, db: float) -> str:
        self.frames += 1
        if len(self.early) < 10 and db > -110:          # background level from the first 200 ms that carry sound
            self.early.append(db)
            if len(self.early) == 10:
                self.threshold = max(-55.0, min(-30.0, sorted(self.early)[2] + 12))
        if not self.speaking:
            self.loud = self.loud + 1 if db > self.threshold else 0
            self.peak = max(self.peak, db) if self.loud > 1 else db
            if self.loud >= START:
                self.speaking = True
                return "start"
            return "give up" if self.frames >= GIVE_UP else "wait"
        self.recent.append(db)
        still = db < self.threshold - 3
        if not still and len(self.recent) == BACKGROUND:
            still = db < min(min(self.recent) + ABOVE_BACKGROUND, self.peak - BELOW_VOICE)
        if still:
            self.quiet += 1
        else:
            self.quiet = 0
            self.peak = max(self.peak, db)
        return "end" if self.quiet >= END else "wait"


class Mic(QObject):
    """One recording at a time. Signals arrive on the GUI thread (Qt queues them across threads)."""
    level = Signal(float)              # 0..1 for a level bar
    status = Signal(str)               # "listening", "sending", "idle"
    result = Signal(dict)              # the agent's answer, or {"error": "..."}

    def __init__(self):
        super().__init__()
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._cancelled = False
        self.busy = False

    def start(self):
        if self.busy:
            self._stop.set()           # pressed again: send what we have
            return
        self.busy = True
        self._stop.clear()
        threading.Thread(target=self._run, name="mic", daemon=True).start()

    def cancel(self):
        self._cancelled = True
        self._stop.set()

    def _run(self):
        self._cancelled = False
        self.status.emit("listening")
        try:
            pcm = self._record()
        except FileNotFoundError:
            self.result.emit({"error": "pw-record (PipeWire) isn't installed"})
            pcm = None
        except Exception as e:
            self.result.emit({"error": f"microphone: {e}"})
            pcm = None
        if pcm is None or self._cancelled:
            self._finish()
            return
        if not pcm:
            self.result.emit({"error": "Didn't hear anything"})
            self._finish()
            return
        self.status.emit("sending")
        try:
            self.result.emit(api("/v1/audio", raw=pcm, timeout=90))
        except ApiError as e:
            self.result.emit({"error": f"The agent refused it: {e}"})
        except OSError:
            self.result.emit({"error": "The voice agent isn't answering. Is it running?"})
        self._finish()

    def _finish(self):
        self.busy = False
        self.level.emit(0.0)
        self.status.emit("idle")

    def _record(self) -> bytes:
        """Raw 16 kHz mono PCM16 of one utterance (b'' if nobody spoke)."""
        self._proc = subprocess.Popen(
            ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "--latency", "20ms", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        speech, preroll, ends = bytearray(), deque(maxlen=PREROLL), Endpointer()
        try:
            while not self._stop.is_set() and ends.frames < MAX:
                frame = self._proc.stdout.read(FRAME_BYTES)
                if len(frame) < FRAME_BYTES:
                    break
                db = level_db(frame)
                self.level.emit(max(0.0, min(1.0, (db + 60) / 45)))
                was_speaking = ends.speaking
                said = ends.feed(db)
                if was_speaking:
                    speech += frame
                else:
                    preroll.append(frame)
                if said == "start":
                    for f in preroll:
                        speech += f
                elif said in ("end", "give up"):
                    break
        finally:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        return bytes(speech) if ends.speaking else b""
