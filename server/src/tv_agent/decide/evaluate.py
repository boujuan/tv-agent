"""Score a router on the labelled sentences in eval/utterances.jsonl (nothing is sent to the TV)."""
import json
import statistics
import time
from pathlib import Path

EVAL_FILE = Path(__file__).resolve().parents[3] / "eval" / "utterances.jsonl"


def matches(action, expect: dict) -> bool:
    got = action.model_dump()
    for key, want in expect.items():
        have = got.get(key)
        if isinstance(want, str) and isinstance(have, str):
            if want.lower().strip() != have.lower().strip():
                return False
        elif have != want:
            return False
    return True


async def run(router, path: Path = EVAL_FILE) -> dict:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    intent_ok = full_ok = 0
    results, by_via = [], {}
    for row in rows:
        t = time.perf_counter()
        d = await router.decide(row["text"])
        ms = (time.perf_counter() - t) * 1000
        by_via.setdefault(d.via, []).append(ms)
        i_ok = d.action.type == row["expect"]["type"]
        f_ok = i_ok and matches(d.action, row["expect"])
        intent_ok += i_ok
        full_ok += f_ok
        results.append({"text": row["text"], "expect": row["expect"], "ok": f_ok, "via": d.via, "rule": d.rule,
                        "confidence": round(d.confidence, 2),
                        "got": {k: v for k, v in d.action.model_dump().items() if v not in (None, False, "")}})
    timing = {}
    for via, xs in by_via.items():
        xs.sort()
        timing[via] = {"n": len(xs), "p50": statistics.median(xs), "p95": xs[int(0.95 * (len(xs) - 1))]}
    return {"n": len(rows), "intent_ok": intent_ok, "full_ok": full_ok, "results": results, "timing": timing}
