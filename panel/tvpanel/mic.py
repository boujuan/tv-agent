"""The laptop's microphone as a second remote: record one command with PipeWire (`pw-record`, the default input),
stop when you stop talking (same end-of-speech rules as the TV app), send it to the agent, return its answer."""
import array
import math
import subprocess
import threading

from PySide6.QtCore import QObject, Signal

from .common import ApiError, api

RATE = 16000
FRAME_BYTES = RATE // 50 * 2           # 20 ms of 16-bit mono
PREROLL, START, END, GIVE_UP, MAX = 15, 3, 35, 200, 500   # frames, as in the TV app


def level_db(frame: bytes) -> float:
    samples = array.array("h", frame)
    if not samples:
        return -120.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
    return 20 * math.log10(rms) if rms > 0 else -120.0


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
        speech, preroll, early = bytearray(), [], []
        frames = loud = quiet = 0
        threshold, speaking = -45.0, False
        try:
            while not self._stop.is_set() and frames < MAX:
                frame = self._proc.stdout.read(FRAME_BYTES)
                if len(frame) < FRAME_BYTES:
                    break
                frames += 1
                db = level_db(frame)
                self.level.emit(max(0.0, min(1.0, (db + 60) / 45)))
                if len(early) < 10 and db > -110:          # background level from the first 200 ms
                    early.append(db)
                    if len(early) == 10:
                        threshold = max(-55.0, min(-30.0, sorted(early)[2] + 12))
                if not speaking:
                    preroll.append(frame)
                    if len(preroll) > PREROLL:
                        preroll.pop(0)
                    loud = loud + 1 if db > threshold else 0
                    if loud >= START:
                        speaking = True
                        for f in preroll:
                            speech += f
                    elif frames >= GIVE_UP:
                        break
                else:
                    speech += frame
                    quiet = quiet + 1 if db < threshold - 3 else 0
                    if quiet >= END:
                        break
        finally:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        return bytes(speech) if speaking else b""
