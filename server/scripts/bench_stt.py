"""Phase 0 gate G4: Parakeet TDT 0.6B v2 (int8, sherpa-onnx, CPU) on the probe takes.

    .venv/bin/python scripts/bench_stt.py phase0/take-*.wav

Prints each transcript and the p50/p95 decode time over 10 runs (after one warm-up).
"""
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

MODEL = Path.home() / ".local/share/tv-agent/models/parakeet-v2/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"


def load(path):
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, path
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return w.getframerate(), pcm.astype(np.float32) / 32768.0


def main(paths):
    t = time.perf_counter()
    rec = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(MODEL / "encoder.int8.onnx"),
        decoder=str(MODEL / "decoder.int8.onnx"),
        joiner=str(MODEL / "joiner.int8.onnx"),
        tokens=str(MODEL / "tokens.txt"),
        model_type="nemo_transducer",
        num_threads=4,
    )
    print(f"load {time.perf_counter() - t:.1f} s")

    def decode(rate, samples):
        s = rec.create_stream()
        s.accept_waveform(rate, samples)
        rec.decode_stream(s)
        return s.result.text

    for path in paths:
        rate, samples = load(path)
        decode(rate, samples)  # warm-up
        times = []
        for _ in range(10):
            t = time.perf_counter()
            text = decode(rate, samples)
            times.append((time.perf_counter() - t) * 1000)
        times.sort()
        print(f"{Path(path).name}  {len(samples) / rate:.1f} s audio  p50 {statistics.median(times):.0f} ms"
              f"  p95 {times[int(0.95 * (len(times) - 1))]:.0f} ms  -> {text!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
