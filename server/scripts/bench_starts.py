"""Starts, timed without the TV: the agent's own code from a decided play request (eval/starts.jsonl) to the link the
player would be given: the title, its metadata, your addons' streams, the ranking and the link check. Your addons
and debrid service answer for real; nothing is sent to the TV and nothing is written to Stremio (a dry run, with the
stream lookups a real start makes ahead of time switched on).

Each row runs in five scenarios, in this order:
  cold    nothing kept by the agent (its stream lists, links and metadata forgotten; AIOStreams' own cache isn't
          ours to empty, so its answer may come from it)
  again   the same request a moment later (what the agent kept)
  soon    the same request as if 10 minutes later (what the agent kept, aged by hand)
  later   the same request as if 31 minutes later (aged past the stream lists' fresh window)
  ahead   nothing kept, but the stream lookup started --ahead seconds before the request, as when the mic opens
          (Continue Watching) or a title is guessed while it's being decided
Each start waits --gap seconds first, as between real commands (idle connections expire meanwhile).

    PYTHONPATH=src .venv/bin/python scripts/bench_starts.py [--rows 1,4] [--only cold,again] [--out results.json]

The same script times older code too (PYTHONPATH at another checkout's src): what it reads that only newer code
has is optional.
"""
import argparse
import asyncio
import json
import math
import statistics
import sys
import time
import types
from pathlib import Path

from tv_agent import actions as A
from tv_agent import agent as agent_mod
from tv_agent import config, tracing
from tv_agent.agent import HEARD, START, Agent
from tv_agent.stremio import cinemeta, streams

EVAL = Path(__file__).resolve().parents[1] / "eval" / "starts.jsonl"
SCENARIOS = ("cold", "again", "soon", "later", "ahead")
calls: list[dict] = []          # each addon lookup and link check of the start being timed


def instrument():
    """Time the agent's calls to the addons (streams.fetch) and to the links (streams.resolve and, in newer code,
    streams.recheck), wherever they're made from."""
    def wrap(name: str):
        fn = getattr(streams, name, None)
        if fn is None:
            return

        async def timed(*args, **kw):
            t = time.perf_counter()
            try:
                return await fn(*args, **kw)
            finally:
                calls.append({"call": name, "ms": round((time.perf_counter() - t) * 1000)})
        setattr(streams, name, timed)
    for name in ("fetch", "resolve", "recheck"):
        wrap(name)


def look_ahead(agent: Agent):
    """The lookups a real start makes ahead (prefetch) also in this dry run."""
    if hasattr(agent, "look_ahead"):
        agent.look_ahead = True
        return
    real = Agent.prefetch                   # older code: prefetch() itself refuses in a dry run

    def prefetch(self, kind, video_id):
        self.dry_run = False
        try:
            return real(self, kind, video_id)
        finally:
            self.dry_run = True
    agent.prefetch = types.MethodType(prefetch, agent)


def forget(agent: Agent):
    agent._streams.clear()
    agent._stream_jobs.clear()
    for name in ("_links", "_link_jobs"):
        getattr(agent, name, {}).clear()
    cinemeta._metas.clear()


def age(agent: Agent, seconds: float):
    """Everything the agent keeps as if `seconds` older."""
    for key, (at, value) in list(agent._streams.items()):
        agent._streams[key] = (at - seconds, value)
    for key, hit in list(getattr(agent, "_links", {}).items()):      # (found, link, last answered)
        agent._links[key] = (hit[0] - seconds, hit[1], *(t - seconds for t in hit[2:]))
    for key, (at, value) in list(cinemeta._metas.items()):
        cinemeta._metas[key] = (at - seconds, value)


async def start(agent: Agent, row: dict) -> dict:
    a = A.PlayTitle(title=row["title"], kind=row.get("kind"), season=row.get("season"), episode=row.get("episode"))
    HEARD.set(row["say"])
    START.set({"_t0": time.perf_counter()})
    trace = tracing.Trace()
    token = tracing.TRACE.set(trace)
    calls.clear()
    t = time.perf_counter()
    try:
        out = await agent.execute(a, row["say"])
    finally:
        tracing.TRACE.reset(token)
    ms = (time.perf_counter() - t) * 1000
    steps, _ = trace.close()
    by = {(s["stage"], s["step"]): s for s in steps}
    got = by.get(("start", "streams"), {})
    return {"ok": out.ok, "say": out.say, "ms": round(ms), "video": got.get("video"), "how": got.get("how"),
            "streams_ms": got.get("ms"), "link_ms": by.get(("start", "link check"), {}).get("ms"),
            "title_ms": by.get(("title", "picked"), {}).get("ms"),
            "lookups": [c["ms"] for c in calls if c["call"] == "fetch"],
            "links": [c["ms"] for c in calls if c["call"] in ("resolve", "recheck")]}


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[max(0, math.ceil(p * len(xs)) - 1)] if xs else 0.0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", help="row numbers, from 1")
    ap.add_argument("--only", help="scenarios, comma-separated (" + ", ".join(SCENARIOS) + ")")
    ap.add_argument("--gap", type=float, default=6.0, help="seconds idle before each start")
    ap.add_argument("--ahead", type=float, default=4.0, help="seconds the lookup starts before the request")
    ap.add_argument("--out")
    args = ap.parse_args()
    rows = [json.loads(line) for line in EVAL.read_text().splitlines() if line.strip()]
    picked = {int(x) for x in args.rows.split(",")} if args.rows else None
    rows = [(n, r) for n, r in enumerate(rows, 1) if picked is None or n in picked]
    scenarios = args.only.split(",") if args.only else SCENARIOS
    instrument()
    agent = Agent(config.load(), dry_run=True)
    look_ahead(agent)
    results = []
    try:
        for n, row in rows:
            video = None
            for run in scenarios:
                if run in ("cold", "ahead"):
                    forget(agent)
                elif run == "soon":
                    age(agent, 600)
                elif run == "later":
                    age(agent, agent_mod.STREAMS_TTL + 60)
                await asyncio.sleep(args.gap)
                if run == "ahead" and video:
                    agent.prefetch("series" if ":" in video else "movie", video)
                    await asyncio.sleep(args.ahead)
                try:
                    r = await start(agent, row)
                except Exception as e:                   # one failure doesn't stop the rest
                    r = {"ok": False, "say": f"{type(e).__name__}: {e}", "ms": 0, "lookups": [], "links": []}
                video = video or r.get("video")
                results.append({"row": n, "run": run, **r})
                print(f"{'✓' if r['ok'] else '✗'} {n:2} {run:5} {r['ms']:6.0f} ms  streams {r.get('streams_ms')} "
                      f"link {r.get('link_ms')}  lookups {r['lookups']} links {r['links']}  {row['say']}"
                      + ("" if r["ok"] else f"  ({r['say'][:100]})"), flush=True)
                await asyncio.sleep(0.5)       # what the start left running (a lookup's refresh) is let finish
    finally:
        await agent.close()
    print()
    for run in scenarios:
        done = [r for r in results if r["run"] == run and r["ok"]]
        if not done:
            continue
        line = [f"{run:5} n={len(done)}"]
        for key in ("ms", "streams_ms", "link_ms"):
            xs = [r[key] for r in done if r.get(key) is not None]
            if xs:
                line.append(f"{key.removesuffix('_ms') if key != 'ms' else 'total'} p50 {statistics.median(xs):5.0f} "
                            f"p95 {pct(xs, .95):5.0f}")
        lookups = [ms for r in done for ms in r["lookups"]]
        line.append(f"addon lookups {len(lookups)} (p50 {statistics.median(lookups):.0f} ms)" if lookups else
                    "addon lookups 0")
        print("  ".join(line))
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
