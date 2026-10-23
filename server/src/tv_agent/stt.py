"""Speech to text: on this laptop through sherpa-onnx on the CPU (`stt_model`), or hosted, through OpenRouter's
transcription API (`stt_hosted_model`); `stt_where` picks which (the panel's Decisions tab, "Speech to text"):

- parakeet-v2: NVIDIA Parakeet TDT 0.6B v2, English (the default). About 0.2 s for 5 s of audio on the Ryzen 7 8845HS.
- parakeet-v3: Parakeet TDT 0.6B v3, 25 European languages including Spanish, the same speed. It works out the
  language by itself (it can't be told one) and is slightly worse at English (WER 6.34 vs 6.05 %).

`stt_model` can also be the path of another sherpa-onnx NeMo transducer folder. Models are downloaded on request
(`tv-agent setup stt parakeet-v3`, or the panel), checked against the SHA-256 GitHub publishes for the archive.
A hosted model gets the recording when you pause (OpenRouter's API isn't streaming): measured 0.4-0.7 s for the
quick ones (docs/research-2026-10-22.md §2.10). Audio is written to disk only with `keep_recordings` on (the last
RECORDINGS_KEPT commands, in ~/.local/share/tv-agent/recordings, for the Activity tab).
"""
import base64
import hashlib
import io
import os
import re
import tarfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR      # stdlib only up to here: the panel reads MODELS

RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"


@dataclass(frozen=True)
class Model:
    label: str
    languages: str
    archive: str          # the sherpa-onnx `asr-models` release asset (without .tar.bz2): also the unpacked folder
    sha256: str
    size: int             # bytes to download


MODELS = {
    "parakeet-v2": Model("Parakeet TDT 0.6B v2", "English", "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
                         "157c157bc51155e03e37d2466522a3a737dd9c72bb25f36eb18912964161e1ad", 482_468_385),
    "parakeet-v3": Model("Parakeet TDT 0.6B v3", "25 European languages, Spanish included",
                         "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
                         "5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf", 487_170_055),
}
FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
# OpenRouter's transcription models that did well on short commands, by name; AssemblyAI first: the best on
# your own recordings with a Spanish hint (2026-10-23: 3 wrong of 30, ~0.25 s), and it takes a vocabulary
HOSTED = {"assemblyai/universal-3-6-pro": "AssemblyAI Universal-3.6 Pro",
          "mistralai/voxtral-mini-transcribe-3-0": "Voxtral Mini Transcribe 3",
          "deepgram/nova-3": "Deepgram Nova-3",
          "mistralai/voxtral-small-transcribe-3-0": "Voxtral Small Transcribe 3",
          "x-ai/grok-stt-1.0": "Grok STT 1.0",
          "elevenlabs/scribe-v2": "ElevenLabs Scribe v2",
          "openai/gpt-4o-mini-transcribe": "GPT-4o mini Transcribe",
          "openai/gpt-transcribe": "GPT Transcribe",
          "microsoft/mai-transcribe-2": "MAI-Transcribe 2",
          "nvidia/parakeet-tdt-0.6b-v3": "Parakeet TDT 0.6B v3 (hosted)",
          "openai/whisper-large-v3-turbo": "Whisper large v3 turbo"}
# Which of them use the language hint (it changed their answer on Spanish commands) and the vocabulary (measured
# 2026-10-23; the others ignore what they don't use: Grok, GPT transcribe, GPT-4o mini, MAI, hosted Parakeet)
USES_LANGUAGE = {"assemblyai/universal-3-6-pro", "mistralai/voxtral-mini-transcribe-3-0", "deepgram/nova-3",
                 "mistralai/voxtral-small-transcribe-3-0", "elevenlabs/scribe-v2", "openai/whisper-large-v3-turbo"}
USES_VOCABULARY = {"assemblyai/universal-3-6-pro"}
TRANSCRIBE_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
VOCABULARY_MAX = 100     # names sent per request: AssemblyAI refuses more (HTTP 400, measured 2026-10-24)
NON_LATIN = re.compile(r"[^\W\d_a-zA-Z\u00C0-\u024F]")   # a letter outside Latin scripts: "Пауса", "Ютуб"
RECORDINGS = DATA_DIR / "recordings"
RECORDINGS_KEPT = 300
RATE = 16000


def wav(pcm16: bytes, rate: int = RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    return out.getvalue()


async def hosted_transcribe(http, key: str, model: str, pcm16: bytes, timeout: float = 6.0, language: str = "",
                            vocabulary: list[str] | None = None) -> tuple[str, float | None]:
    """(text, cost in $) from one of OpenRouter's transcription models; raises httpx errors. `language`: an ISO 639-1
    hint. `vocabulary`: names to hear, passed to AssemblyAI as its keyterms (OpenRouter's own "keyterms" field isn't
    taken by these models; measured 2026-10-23: "Better Console" -> "Better Call Saul", "este remedio" -> "Stremio")."""
    body = {"model": model, "input_audio": {"data": base64.b64encode(wav(pcm16)).decode(), "format": "wav"}}
    if language:
        body["language"] = language
    if vocabulary and model.startswith("assemblyai/"):
        body["provider"] = {"options": {"assemblyai": {"keyterms_prompt": vocabulary[:VOCABULARY_MAX]}}}
    r = await http.post(TRANSCRIBE_URL, timeout=timeout, headers={"Authorization": f"Bearer {key}", "X-Title": "tv-agent"},
                        json=body)
    r.raise_for_status()
    data = r.json()
    return str(data.get("text") or "").strip(), (data.get("usage") or {}).get("cost")


def save_recording(pcm16: bytes) -> str | None:
    """Keep a command's audio (with keep_recordings on): its file name, the oldest beyond RECORDINGS_KEPT removed."""
    if len(pcm16) < RATE // 5:                          # under 0.1 s: nothing was said
        return None
    RECORDINGS.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.wav"
    fd = os.open(RECORDINGS / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # private from the start
    with os.fdopen(fd, "wb") as f:
        f.write(wav(pcm16))
    for old in sorted(RECORDINGS.glob("*.wav"))[:-RECORDINGS_KEPT]:
        old.unlink(missing_ok=True)
    return name


def recordings() -> list[Path]:
    return sorted(RECORDINGS.glob("*.wav")) if RECORDINGS.is_dir() else []


def delete_recordings() -> int:
    """Every kept recording removed (keep_recordings turned off, or the history erased): how many."""
    gone = recordings()
    for path in gone:
        path.unlink(missing_ok=True)
    return len(gone)


def recording(name: str) -> Path | None:
    """A kept recording by its file name (nothing outside the folder)."""
    path = RECORDINGS / Path(name).name
    return path if path.suffix == ".wav" and path.exists() else None


def read_recording(name: str) -> bytes | None:
    path = recording(name)
    if not path:
        return None
    with wave.open(str(path)) as w:
        return w.readframes(w.getnframes())


def model_dir(choice: str) -> Path:
    """A model's folder: one of MODELS by name, or a path."""
    m = MODELS.get(choice)
    return DATA_DIR / "models" / choice / m.archive if m else Path(choice).expanduser()


def installed(choice: str) -> bool:
    return all((model_dir(choice) / f).exists() for f in FILES)


def download(name: str, http, progress=lambda done, total: None) -> Path:
    """Fetch, check and unpack one of MODELS (resumable is not worth it: ~0.5 GB from GitHub's CDN)."""
    m = MODELS[name]
    folder = DATA_DIR / "models" / name
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / f"{m.archive}.tar.bz2.part"
    digest, done = hashlib.sha256(), 0
    with http.stream("GET", RELEASE + m.archive + ".tar.bz2", follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress(done, m.size)
    if digest.hexdigest() != m.sha256:
        part.unlink()
        raise ValueError(f"{m.archive}: the download doesn't match its published SHA-256; nothing was installed")
    with tarfile.open(part, "r:bz2") as tar:
        tar.extractall(folder, filter="data")          # no paths outside the folder, no devices or links out
    part.unlink()
    if not installed(name):
        raise ValueError(f"{m.archive}: unpacked, but {', '.join(FILES)} aren't all there")
    return model_dir(name)


class Stt:
    def __init__(self, model: Path, threads: int = 4):
        import sherpa_onnx
        missing = [f for f in FILES if not (model / f).exists()]
        if missing:   # sherpa-onnx ends the whole process on a missing file instead of raising
            raise FileNotFoundError(f"speech model {model}: missing {', '.join(missing)}")
        self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(model / "encoder.int8.onnx"),
            decoder=str(model / "decoder.int8.onnx"),
            joiner=str(model / "joiner.int8.onnx"),
            tokens=str(model / "tokens.txt"),
            model_type="nemo_transducer",
            num_threads=threads,
        )
        self._lock = threading.Lock()
        self.transcribe(b"\0\0" * 8000)   # warm-up

    def transcribe(self, pcm16: bytes, rate: int = 16000) -> str:
        import numpy as np
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        with self._lock:
            stream = self._rec.create_stream()
            stream.accept_waveform(rate, samples)
            self._rec.decode_stream(stream)
            return stream.result.text.strip()
