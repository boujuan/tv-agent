"""Finding an episode or a scene from a description (eval/finding.jsonl), timed: the agent's own code with your
addons and the LLM, nothing sent to the TV (the jump is caught, the message on the TV is printed). Each row runs
twice: cold (nothing kept from before: subtitles, plots) and warm (what the first run kept).

    .venv/bin/python scripts/bench_finding.py [--only episode|scene] [--rows 1,4] [--out results.json]

A row's "expect": episodes, the right episode ids, or "ask" when several fit and a list should be shown; scenes,
the window [from, to] in seconds where the moment starts. Empty: not graded, printed to check by hand.
"""
import argparse
import asyncio
import json
import math
import statistics
import sys
import tempfile
import time
from pathlib import Path

from tv_agent import actions as A
from tv_agent import chain, config, tracing
from tv_agent import voicetests as V
from tv_agent.agent import Agent, Outcome
from tv_agent.state import NowPlaying
from tv_agent.stremio import cinemeta

EVAL = Path(__file__).resolve().parents[1] / "eval" / "finding.jsonl"


def forget(agent: Agent, keep: Path):
    """Nothing kept from before: subtitles in memory, plots and subtitles on disk (a fresh folder)."""
    agent._episode_cues.clear()
    try:
        from tv_agent import finding
        finding.CACHE = keep / f"cold-{time.monotonic_ns()}"
        finding.forget()
    except ImportError:
        pass


async def episode(agent: Agent, row: dict) -> tuple[str, str]:
    meta = await cinemeta.meta(agent.http, "series", row["imdb"])
    eps = cinemeta.episodes(meta)
    if hasattr(agent, "find_episode"):
        got = await agent.find_episode(row["imdb"], row["name"], eps, row["wanted"])
        if got.episode:
            return got.episode["id"], got.why
        return ("ask" if got.choices else "none"), " · ".join(v["id"] for v in got.choices) or got.why
    v = await agent._find_episode(row["name"], eps, row["wanted"])
    return (v["id"], "") if v else ("none", "")


async def scene(agent: Agent, row: dict) -> tuple[str, str]:
    video = row["video"]
    imdb = video.split(":")[0]
    agent.state.now_playing = NowPlaying(imdb, "series" if ":" in video else "movie", video, row["name"], "tvplayer")
    shown, jumped = [], []

    async def notify(text):
        shown.append(text)

    async def transport(a):
        jumped.append(a.seconds)
        return Outcome(True, "To there")
    agent.notify, agent._transport = notify, transport
    out = await agent._find_scene(A.FindScene(what=row["wanted"]))
    at = (out.details or {}).get("scene_ms")
    if not out.ok and getattr(agent, "_shown", None) and agent._shown.items and agent._shown.items[0].at_ms:
        places = [round(i.at_ms / 1000) for i in agent._shown.items]          # listed to pick from
        agent._shown = None
        return "ask", f"{out.say}: {places}"
    return (str(round(at / 1000)) if out.ok and at is not None else "none"), out.say


def graded(row: dict, got: str) -> bool | None:
    want = row.get("expect") or []
    if not want:
        return None
    if row["find"] == "episode":
        return got in want
    return got.isdigit() and want[0] <= int(got) <= want[1] or got == "ask" and None


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[max(0, math.ceil(p * len(xs)) - 1)] if xs else 0.0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["episode", "scene"])
    ap.add_argument("--rows", help="row numbers, from 1")
    ap.add_argument("--out")
    ap.add_argument("--log", action="store_true", help="the agent's own log lines (each stage's time)")
    args = ap.parse_args()
    if args.log:
        import logging
        logging.basicConfig(level=logging.INFO, format="    %(name)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
    rows = [json.loads(line) for line in EVAL.read_text().splitlines() if line.strip()]
    rows += [r | {"mine": True} for r in V.cases(V.FINDING) if r.get("find") in ("episode", "scene")]
    picked = {int(x) for x in args.rows.split(",")} if args.rows else None
    rows = [(n, r) for n, r in enumerate(rows, 1) if (not args.only or r["find"] == args.only)
            and (picked is None or n in picked)]
    agent = Agent(config.load(), dry_run=True)
    chain.SCENE_SHOWN = 0
    results, times = [], {}
    keep = Path(tempfile.mkdtemp(prefix="bench-finding-"))
    try:
        for n, row in rows:
            forget(agent, keep)
            for run in ("cold", "warm"):
                t = time.perf_counter()
                trace = tracing.Trace()
                token = tracing.TRACE.set(trace)
                try:
                    got, why = await (episode if row["find"] == "episode" else scene)(agent, row)
                except Exception as e:                   # one failure doesn't stop the rest
                    got, why = "error", f"{type(e).__name__}: {e}"
                finally:
                    tracing.TRACE.reset(token)
                ms = (time.perf_counter() - t) * 1000
                ok = graded(row, got)
                steps = trace.close()[0]
                cost = sum(s.get("cost") or 0 for s in steps if s["stage"] in ("llm", "d1"))
                # nothing to search because a subtitle addon failed: not the agent's answer, so not graded
                down = [s["why"] for s in steps if s.get("step") == "subtitles" and s.get("verdict") == "failed"]
                if got in ("none", "error") and down:
                    ok, why = None, f"couldn't be checked: subtitles {down[0]}"
                times.setdefault((row["find"], run), []).append(ms)
                results.append({"row": n, "run": run, "wanted": row["wanted"], "got": got, "ok": ok,
                                "ms": round(ms), "why": why, "cost": round(cost, 6), "mine": row.get("mine"),
                                **({"unavailable": why} if down and ok is None and got in ("none", "error") else {})})
                mark = {True: "✓", False: "✗", None: "·"}[ok]
                print(f"{mark} {n:2} {row['find']:7} {run} {ms:6.0f} ms  {row['name']}: {row['wanted']!r} -> {got}"
                      + (f"  ({why[:140]})" if why else ""), flush=True)
    finally:
        await agent.close()
    print()
    for (kind, run), xs in sorted(times.items()):
        right = [r["ok"] for r in results if r["run"] == run and rows_kind(rows, r["row"]) == kind and r["ok"] is not None]
        print(f"{kind:7} {run}: p50 {statistics.median(xs):6.0f} ms  p95 {pct(xs, .95):6.0f} ms  "
              f"right {sum(right)}/{len(right)} graded")
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


def rows_kind(rows, n):
    return next(r["find"] for m, r in rows if m == n)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
