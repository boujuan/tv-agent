"""How each decision path and model does on what the rules don't catch: accuracy, delay and cost. Nothing is sent to
the TV.

Sentences (--sets): hard (eval/hard.jsonl: 40 English + 40 Spanish indirect phrasings, titles described instead of
named, non-commands; the rules and the LLM's prompt were tuned on it), holdout (eval/hard_holdout.jsonl: 20 + 20
more that nothing was tuned on), voice (eval/voice.jsonl: real commands said to the TV, misheard ones included) and,
with --sample N, N rows per category of eval/commands.jsonl.

Paths:
  rules              clean-up, fixed phrases, patterns and two-command splits only
  rules+d1           then the local d1-3B
  rules+hosted:M     then the hosted decision model M: d1 or d1-ultra through Liquid's API, or any OpenRouter
                     decision model by id (liquid/d1, typesafe/jev-1.14, cloudflare/clef…)
  hosted:M, d1       a decision model on its own on the cleaned-up sentence (no rules); "top-1" also counts
                     its first choice when it wasn't sure enough to act
  llm:MODEL          the LLM on its own (MODEL@effort sets its reasoning effort; MODEL@ sends none)
  pipeline:MODEL     the rules, then that LLM (worked out from the two above: the rules' answer when they have one)

Uses the keys in secrets.toml. Every OpenRouter call reports its cost; Liquid's is estimated from its price per
token. The models run side by side, each one sentence at a time.
    .venv/bin/python scripts/bench_fallbacks.py --sets holdout,voice --models google/gemini-3.5-flash-lite@minimal,
        deepseek/deepseek-v4.1-flash-1017@none --hosted d1,d1-ultra [--local] [--sample 2] [--out results.json]
"""
import argparse
import asyncio
import json
import math
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
from tv_agent.decide.llm import Llm
from tv_agent.decide.router import Router, from_intent, normalize
from tv_agent.stremio.cinemeta import norm

EVAL = Path(__file__).resolve().parents[1] / "eval"
SETS = {"hard": "hard.jsonl", "holdout": "hard_holdout.jsonl", "voice": "voice.jsonl"}
LIQUID_PRICE = {"d1": 0.02e-6, "d1-ultra": 0.042e-6}      # $ per input token (docs/research-2026-10-22.md §2.6)


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


def rows(sets: list[str], sample: int) -> list[dict]:
    out = []
    for name in sets:
        out += [json.loads(line) | {"set": name} for line in (EVAL / SETS[name]).read_text().splitlines() if line]
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


class NoD1:
    where = "local"

    async def choice(self, *args):
        raise httpx.ConnectError("off")


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sets", default="hard,holdout,voice")
    p.add_argument("--sample", type=int, default=0)
    p.add_argument("--models", default="", help="comma-separated OpenRouter ids, each MODEL@reasoning")
    p.add_argument("--hosted", default="", help="hosted decision models: d1,d1-ultra (Liquid's API) or OpenRouter "
                                                "ids (typesafe/jev-1.14,cloudflare/clef,…)")
    p.add_argument("--local", action="store_true", help="the local d1-3B too")
    p.add_argument("--out")
    args = p.parse_args()
    cfg = config.load()
    agent = Agent(cfg)
    http = agent.http
    rules = R.load_safe()[0]
    rules.fallback_intent = "none"                       # no "play it as a title" guess when no model answered
    data = rows(args.sets.split(","), args.sample)
    options = {k: v["describe"] for k, v in rules.intents.items()}
    results = [{"text": r["text"], "lang": r["lang"], "set": r["set"], "expect": r["expect"]} for r in data]

    def record(i: int, path: str, action: A.Action, via: str, ms: float, cost: float | None = None,
               top1: A.Action | None = None):
        kind_ok, full_ok = judge(action, data[i]["expect"])
        results[i][path] = {"ok": full_ok, "kind": kind_ok, "ms": round(ms), "via": via, "cost": cost,
                            "got": {k: v for k, v in action.model_dump().items() if v not in (None, False, "", 0)}}
        if top1 is not None:
            results[i][path]["top1"] = judge(top1, data[i]["expect"])[1]

    def d1_cost(d1: D1, model: str, text: str) -> float | None:
        if (d1.last_usage or {}).get("cost") is not None:
            return d1.last_usage["cost"]                  # OpenRouter reports it
        return len(json.dumps({"s": text, "o": options})) / 4 * LIQUID_PRICE[model] if model in LIQUID_PRICE else None

    async def run_router(path: str, router: Router):
        for i, r in enumerate(data):
            t = time.perf_counter()
            if isinstance(router.d1, D1):
                router.d1.last_usage = {}
            d = await router.decide(r["text"])
            cost = None
            if path.startswith("rules+hosted:") and d.via == "d1":
                cost = d1_cost(router.d1, path.split(":", 1)[1], normalize(r["text"], rules))
            record(i, path, d.action, d.via, (time.perf_counter() - t) * 1000, cost)

    async def run_d1(path: str, d1: D1, model: str):
        for i, r in enumerate(data):
            text = normalize(r["text"], rules)
            top1 = None
            for attempt in range(3):                     # a rate limit or a hiccup: twice more, a second apart
                t = time.perf_counter()
                d1.last_usage = {}
                try:
                    pick = await d1.choice(text, rules.question, options)
                    top = sorted(pick.probabilities.values(), reverse=True)
                    ratio = top[0] / max(top[1], 1e-9) if len(top) > 1 else float("inf")
                    ok = pick.key != "none" and pick.confidence >= rules.min_confidence and ratio >= rules.min_ratio
                    top1 = (from_intent(pick.key, text, rules) if pick.key != "none" else None) or A.Unclear(heard=text)
                    action = (top1 if ok else None) or A.Unclear(heard=r["text"])
                    via = path
                    break
                except Exception as e:                   # one model's failure doesn't stop the others
                    action, via = A.Unclear(heard=r["text"]), f"error {type(e).__name__}"
                    await asyncio.sleep(1)
            record(i, path, action, via, (time.perf_counter() - t) * 1000, d1_cost(d1, model, text), top1)
            print(f"\r{path[:40]:40} {i + 1}/{len(data)}", end="", file=sys.stderr, flush=True)

    async def run_llm(spec: str):
        model, _, reasoning = spec.partition("@")
        llm = Llm(http, cfg.secret("openrouter_api_key"), model, rules, reasoning)
        for i, r in enumerate(data):
            t = time.perf_counter()
            llm.last_usage = {}
            try:
                reached, action = await llm.answer(r["text"], await agent.llm_context(r["text"]))
                via = "llm" if reached else "unreachable"
            except Exception as e:                      # one model's failure doesn't stop the others
                reached, action, via = False, None, f"error {type(e).__name__}"
            record(i, f"llm:{spec}", action or A.Unclear(heard=r["text"]), via, (time.perf_counter() - t) * 1000,
                   (llm.last_usage or {}).get("cost"))
            print(f"\r{spec[:40]:40} {i + 1}/{len(data)}", end="", file=sys.stderr, flush=True)

    jobs = [run_router("rules", Router(NoD1(), rules, off={"llm", "d1"}))]
    if args.local:
        local = D1(http, cfg.d1_url, where="local")
        jobs += [run_router("rules+d1", Router(local, rules, off={"llm"})), run_d1("d1", local, "")]
    key, or_key = cfg.secret("liquid_api_key"), cfg.secret("openrouter_api_key")
    for m in filter(None, args.hosted.split(",")):
        hosted = D1(http, cfg.d1_url, key, m, where="hosted", openrouter_key=or_key)
        jobs += [run_router(f"rules+hosted:{m}", Router(hosted, rules, off={"llm"})),
                 run_d1(f"hosted:{m}", D1(http, cfg.d1_url, key, m, where="hosted", openrouter_key=or_key), m)]
    models = [m for m in args.models.split(",") if m]
    jobs += [run_llm(spec) for spec in models]
    await asyncio.gather(*jobs)
    print(file=sys.stderr)
    for spec in models:                                  # the rules first, then the LLM: as deployed
        for res in results:
            rule, llm = res["rules"], res[f"llm:{spec}"]
            res[f"pipeline:{spec}"] = rule if rule["via"] not in ("none",) else llm
    await agent.close()

    paths = [k for k in results[0] if ":" in k or k in ("rules", "rules+d1", "d1")]
    paths = sorted(paths, key=lambda k: (k.startswith("llm:"), k.startswith("pipeline:"), k))

    def line(path: str, subset: list[dict]) -> str:
        n = len(subset)
        ok = sum(x[path]["ok"] for x in subset)
        kind = sum(x[path]["kind"] for x in subset)
        # delay only where this path's own step ran (the rules' instant answers would hide it)
        own = sorted(x[path]["ms"] for x in subset if x[path]["via"] not in ("phrase", "pattern", "split", "none")) \
            or [0]
        costs = [x[path]["cost"] for x in subset if x[path].get("cost") is not None]
        per_1000 = f"${1000 * statistics.mean(costs):.3f}/1000 asked" if costs else ""
        top1 = [x[path]["top1"] for x in subset if "top1" in x[path]]
        top1 = f"  top-1 {100 * sum(top1) / len(top1):3.0f}%" if top1 else ""
        errors = sum(str(x[path]["via"]).startswith("error") for x in subset)
        errors = f"  {errors} errors" if errors else ""
        return (f"{ok:3}/{n} ({100 * ok / n:3.0f}%), kind {kind:3}/{n}, delay p50 {statistics.median(own):5.0f} ms "
                f"p95 {own[max(0, math.ceil(0.95 * len(own)) - 1)]:5.0f} ms  {per_1000}{top1}{errors}")

    for set_name in [*args.sets.split(","), "sample"]:
        for lang in ("en", "es"):
            subset = [x for x in results if x["set"] == set_name and x["lang"] == lang]
            if not subset:
                continue
            print(f"\n{set_name}, {'English' if lang == 'en' else 'Spanish'} ({len(subset)})")
            for path in paths:
                print(f"  {path[:52]:52} {line(path, subset)}")
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
