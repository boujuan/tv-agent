"""Chained commands (eval/chains.jsonl) through the decision steps set up as the running agent has them (rules,
taught sentences, steps on and off, the LLM): each sentence's chain, single command or refusal, judged step by step.
Nothing is sent to the TV; the LLM is called (and costs) as in use (`--off llm`: rules only).

    .venv/bin/python scripts/bench_chains.py [--off llm] [--failures] [--out results.json] [--no-mine]

Your own sentences (the panel's "This was right / wrong" for typed commands, in ~/.local/share/tv-agent/voice-tests/
sentences.jsonl, never in the repo) are judged too, unless --no-mine. --out keeps each row's result (the nightly
check compares them with its baseline).
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from bench_voice import judge_label          # this script's folder is on the path when it runs

from tv_agent import config
from tv_agent import voicetests as V
from tv_agent.agent import Agent
from tv_agent.decide import learned
from tv_agent.decide.router import Router, normalize

EVAL = Path(__file__).resolve().parents[1] / "eval" / "chains.jsonl"


def show(action) -> str:
    if action.type == "sequence":
        return " → ".join(show(s) for s in action.steps)
    fields = {k: v for k, v in action.model_dump().items() if k != "type" and v not in (None, False, "")}
    return action.type + (f" {fields}" if fields else "")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--off", default="", help="decision steps switched off too (phrases, patterns, llm, d1)")
    ap.add_argument("--failures", action="store_true", help="only what's wrong")
    ap.add_argument("--out", help="each row's result, as JSON")
    ap.add_argument("--no-mine", action="store_true", help="without your own sentences")
    args = ap.parse_args()
    cfg = config.load()
    agent = Agent(cfg, dry_run=True)
    rules = agent.rules
    for x in learned.load():
        rules.phrases[normalize(x["say"], rules)] = (x["do"], "learned")
    off = {*cfg.decide_off, *filter(None, args.off.split(","))}
    router = Router(agent.d1, rules, agent.llm, agent.llm_context, cfg.decide_steps, off)
    rows = [json.loads(line) for line in EVAL.read_text().splitlines() if line.strip()]
    rows += [] if args.no_mine else [r | {"mine": True} for r in V.cases(V.SENTENCES) if r.get("text")]
    right, cost, results = 0, 0.0, []
    try:
        for row in rows:
            d = await router.decide(row["text"])
            ok = judge_label(d.action, row["expect"], False)[1]
            right += ok
            cost += d.cost
            results.append({"text": row["text"], "ok": ok, "got": show(d.action), "via": d.via, "cost": d.cost,
                            "mine": row.get("mine")})
            if ok and args.failures:
                continue
            print(f"{'✓' if ok else '✗'} {row['text']}\n    {d.via}: {show(d.action)}"
                  + (f"\n    note: {d.note}" if d.note else ""))
    finally:
        await agent.close()
    print(f"\n{right}/{len(rows)} right · models ${cost:.4f}", file=sys.stderr)
    if args.out:
        Path(args.out).write_text(json.dumps({"right": right, "of": len(rows), "cost": round(cost, 5),
                                              "rows": results}, ensure_ascii=False, indent=1))
    return 0 if right == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
