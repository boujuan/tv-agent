"""Speech to text through sherpa-onnx on the CPU, with the model chosen in config.toml (`stt_model`, or the panel):

- parakeet-v2: NVIDIA Parakeet TDT 0.6B v2, English (the default). About 0.2 s for 5 s of audio on the Ryzen 7 8845HS.
- parakeet-v3: Parakeet TDT 0.6B v3, 25 European languages including Spanish, the same speed. It works out the
  language by itself (it can't be told one) and is slightly worse at English (WER 6.34 vs 6.05 %).

`stt_model` can also be the path of another sherpa-onnx NeMo transducer folder. Models are downloaded on request
(`tv-agent setup stt parakeet-v3`, or the panel), checked against the SHA-256 GitHub publishes for the archive.
Audio is never written to disk.
"""
import hashlib
import tarfile
import threading
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
    "parakeet-v3": Model("Parakeet TDT 0.6B v3", "25 European languages, Spanish included (found by itself)",
                         "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
                         "5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf", 487_170_055),
}
FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")


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
