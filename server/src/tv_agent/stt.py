"""Speech to text: NVIDIA Parakeet TDT 0.6B v2 (English, int8) through sherpa-onnx on the CPU.
About 0.2 s for 5 s of audio on the Ryzen 7 8845HS. Audio is never written to disk."""
import threading
from pathlib import Path

import numpy as np
import sherpa_onnx


class Stt:
    def __init__(self, model_dir: Path, threads: int = 4):
        self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(model_dir / "encoder.int8.onnx"),
            decoder=str(model_dir / "decoder.int8.onnx"),
            joiner=str(model_dir / "joiner.int8.onnx"),
            tokens=str(model_dir / "tokens.txt"),
            model_type="nemo_transducer",
            num_threads=threads,
        )
        self._lock = threading.Lock()
        self.transcribe(b"\0\0" * 8000)   # warm-up

    def transcribe(self, pcm16: bytes, rate: int = 16000) -> str:
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        with self._lock:
            stream = self._rec.create_stream()
            stream.accept_waveform(rate, samples)
            self._rec.decode_stream(stream)
            return stream.result.text.strip()
