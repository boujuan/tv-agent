"""How each decision path does on what the rules don't catch, and how long it takes. Nothing is sent to the TV.

Sentences: eval/hard.jsonl (40 English + 40 Spanish: indirect phrasings, titles described instead of named,
non-commands; the rules and the LLM's prompt were tuned on it), eval/hard_holdout.jsonl (20 + 20 more that nothing
was tuned on: --hard eval/hard_holdout.jsonl) and, with --sample N, N rows per category of eval/commands.jsonl.

Paths:
  rules          clean-up, fixed phrases and patterns only
  rules+d1       then the local d1-3B (as with no cloud keys)
  rules+hosted   then Liquid's hosted d1 instead (as when the local d1 is down)
  pipeline       then the LLM, d1 only when it can't be reached (as deployed with an OpenRouter key)
  d1-then-llm    then the local d1, the LLM only for what d1 leaves undecided (llm_first off)
  llm            the LLM on its own
  d1, hosted     d1 on its own on the cleaned-up sentence (no rules)

Uses the keys in secrets.toml (a full run costs about $0.2 at OpenRouter, a cent at Liquid).
    .venv/bin/python scripts/bench_fallbacks.py [--sample 2] [--paths rules,pipeline,llm] [--out results.json]
"""
import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path

import httpx

from tv_agent import actions as A
from tv_agent import config
from tv_agent.agent import Agent
from tv_agent.decide import rules as R
from tv_agent.decide.d1 import D1
from tv_agent.decide.router import Router, from_intent, match_rules, normalize
from tv_agent.stremio.cinemeta import norm

EVAL = Path(__file__).resolve().parents[1] / "eval"
PATHS = ["rules", "rules+d1", "rules+hosted", "pipeline", "d1-then-llm", "llm", "d1", "hosted"]


def judge(action: A.Action, expect: dict) -> tuple[bool, bool]:
    """(right kind of action, completely right)."""
    got = action.model_dump()
    if got["type"] != expect["type"]:
        return False, False
    for key, want in expect.items():
        if key == "type":
            continue
        if key in ("title_any", "query_any"):
            have = norm(got.get(key.removesuffix("_any")) or "")
            if not any(norm(w) in have for w in want):
                return True, False
        elif isinstance(want, str) and isinstance(got.get(key), str):
            if want.lower().strip() != got[key].lower().strip():
                return True, False
        elif got.get(key) != want:
            return True, False
    return True, True


def rows(sample: int, hard: Path) -> list[dict]:
    out = [json.loads(line) | {"set": "hard"} for line in hard.read_text().splitlines() if line]
    if sample:
        by_cat: dict[str, list[dict]] = {}
        for line in (EVAL / "commands.jsonl").read_text().splitlines():
            r = json.loads(line)
            by_cat.setdefault(r["cat"], []).append(r)
        rng = random.Random(7)
        for cat, rs in sorted(by_cat.items()):
            for r in rng.sample(rs, min(sample, len(rs))):
                out.append({"lang": "es" if cat == "spanish" else "en", "text": r["text"], "expect": r["expect"],
                            "set": "sample"})
    return out


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sample", type=int, default=0)
    p.add_argument("--paths", default=",".join(PATHS))
    p.add_argument("--out")
    p.add_argument("--hard", default=str(EVAL / "hard.jsonl"),
                   help="the hard sentences (eval/hard_holdout.jsonl: 40 more nothing was tuned on)")
    args = p.parse_args()
    paths = args.paths.split(",")
    cfg = config.load()
    agent = Agent(cfg)
    rules = R.load_safe()[0]
    http = agent.http
    local = D1(http, cfg.d1_url)
    hosted = D1(http, "http://127.0.0.1:9", hosted_key=cfg.secret("liquid_api_key"))   # nothing listens on :9
    llm = agent.llm
    if llm is None and any(x in paths for x in ("pipeline", "llm")):
        sys.exit("no LLM: set an OpenRouter key (tv-agent setup openrouter) and llm_fallback")
    routers = {"rules+d1": Router(local, rules), "rules+hosted": Router(hosted, rules),
               "pipeline": Router(local, rules, llm, agent.llm_context, llm_first=True),
               "d1-then-llm": Router(local, rules, llm, agent.llm_context, llm_first=False)}
    options = {k: v["describe"] for k, v in rules.intents.items()}

    async def d1_only(d1: D1, text: str) -> A.Action:
        t = normalize(text, rules)
        pick = await d1.choice(t, rules.question, options)
        top = sorted(pick.probabilities.values(), reverse=True)
        ratio = top[0] / max(top[1], 1e-9) if len(top) > 1 else float("inf")
        if pick.key == "none" or pick.confidence < rules.min_confidence or ratio < rules.min_ratio:
            return A.Unclear(heard=text)
        return from_intent(pick.key, t, rules) or A.Unclear(heard=text)

    async def llm_only(text: str) -> A.Action:
        return await llm.decide(text, await agent.llm_context(text)) or A.Unclear(heard=text)

    results = []
    data = rows(args.sample, Path(args.hard))
    for i, r in enumerate(data, 1):
        res = {"text": r["text"], "lang": r["lang"], "set": r["set"], "expect": r["expect"]}
        for path in paths:
            t = time.perf_counter()
            try:
                if path == "rules":
                    hit = match_rules(normalize(r["text"], rules), rules, [])
                    action, via = (hit[0], hit[1]) if hit else (A.Unclear(heard=r["text"]), "none")
                elif path in routers:
                    d = await routers[path].decide(r["text"])
                    action, via = d.action, d.via
                elif path == "llm":
                    action, via = await llm_only(r["text"]), "llm"
                else:
                    action, via = await d1_only(local if path == "d1" else hosted, r["text"]), path
            except (httpx.HTTPError, KeyError, ValueError) as e:
                action, via = A.Unclear(heard=r["text"]), f"error {type(e).__name__}"
            ms = (time.perf_counter() - t) * 1000
            kind_ok, full_ok = judge(action, r["expect"])
            res[path] = {"ok": full_ok, "kind": kind_ok, "ms": round(ms), "via": via,
                         "got": {k: v for k, v in action.model_dump().items() if v not in (None, False, "", 0)}}
        results.append(res)
        print(f"\r{i}/{len(data)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    await agent.close()

    def line(path: str, subset: list[dict]) -> str:
        n = len(subset)
        ok = sum(x[path]["ok"] for x in subset)
        kind = sum(x[path]["kind"] for x in subset)
        # delay only where this path's own step ran (the rules' instant answers would hide it)
        own = [x[path]["ms"] for x in subset if x[path]["via"] not in ("phrase", "pattern")] or [0]
        own.sort()
        return (f"{ok:3}/{n} right ({100 * ok / n:3.0f}%), kind right {kind:3}/{n}, delay p50 "
                f"{statistics.median(own):5.0f} ms p95 {own[int(0.95 * (len(own) - 1))]:5.0f} ms")

    for name, subset in (("hard, English", [x for x in results if x["set"] == "hard" and x["lang"] == "en"]),
                         ("hard, Spanish", [x for x in results if x["set"] == "hard" and x["lang"] == "es"]),
                         ("sample, English", [x for x in results if x["set"] == "sample" and x["lang"] == "en"]),
                         ("sample, Spanish", [x for x in results if x["set"] == "sample" and x["lang"] == "es"])):
        if not subset:
            continue
        print(f"\n{name} ({len(subset)})")
        for path in paths:
            print(f"  {path:13} {line(path, subset)}")
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1))



if __name__ == "__main__":
    asyncio.run(main())
