"""Real spoken commands, replayed: each voice test (a recording with what it should do, tv_agent/voicetests.py)
goes through speech to text and the decision steps set up as the running agent has them, and is judged against its
label. Nothing is sent to the TV; the LLM and the hosted models are called (and cost) as in use.

    .venv/bin/python scripts/bench_voice.py [--seed] [--stt local,mistralai/voxtral-mini-transcribe-3-0]
        [--compare before.json] [--save before.json] [--off llm,d1] [--no-learned] [--failures]

--seed        first label the kept recordings that have no label yet, from the history: the action decided, when
              it worked and wasn't marked wrong, or what the panel taught for it (voicetests.expected)
--stt         local (stt_model), parakeet-v2 / parakeet-v3 (that model on this laptop), an OpenRouter transcription
              model id (sent the language hint and names the agent sends: stt_language, its vocabulary and your
              library's titles; MODEL@es, MODEL@en, MODEL@none: with that hint and its names instead), heard (what
              the agent heard back then: the decision steps only) or text (the label's words)
--off         decision steps switched off on top of the agent's own decide_off (phrases, patterns, llm, d1)
--no-learned  without the sentences taught in the panel (a taught recording otherwise matches its own sentence)
--transcripts  reuse a saved run's transcripts (same --stt names): the decision steps alone, without speech to
              text's cost or its answers changing from one run to the next
--save, --compare  keep a run's results, or show the regressions and fixes since one (exits 1 on a regression).
              A bare file name is kept with the voice tests, out of the repo: the transcripts are yours.
"""
import argparse
import asyncio
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import httpx

from bench_fallbacks import judge         # this script's folder is on the path when it runs

from tv_agent import __version__, activity, config, server
from tv_agent import stt as stt_mod
from tv_agent import voicetests as V
from tv_agent.agent import Agent
from tv_agent.decide import learned
from tv_agent.decide.router import STEPS, Router, normalize


def judge_label(action, expect: dict, exact: bool) -> tuple[bool, bool]:
    """judge() (right kind, completely right), and with exact also every field the label doesn't name at its
    default; a sequence step by step."""
    kind, ok = judge(action, {k: v for k, v in expect.items() if k != "steps"})
    if not ok:
        return kind, False
    if isinstance(expect.get("steps"), list):
        steps = expect["steps"]
        return True, len(steps) == len(action.steps) and all(
            isinstance(s, dict) and judge_label(a, s, exact)[1] for a, s in zip(action.steps, steps))
    if exact:
        named = {k.removesuffix("_any") for k in expect}
        for name, field in type(action).model_fields.items():
            if name in named or name in V.UNCHECKED or field.is_required():
                continue
            if getattr(action, name) != field.get_default(call_default_factory=True):
                return True, False
    return True, True


def pcm_of(path: Path) -> tuple[bytes, float]:
    with wave.open(str(path)) as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, stt_mod.RATE):
            raise ValueError(f"{path.name}: not 16 kHz 16-bit mono")
        return w.readframes(w.getnframes()), w.getnframes() / w.getframerate()


def place(arg: str) -> Path:
    """--save / --compare: a bare file name lives with the voice tests."""
    return Path(arg).expanduser() if os.sep in arg else V.DIR / arg


def commit() -> str | None:
    try:
        here = Path(__file__).resolve().parent
        rev = subprocess.run(["git", "describe", "--always", "--dirty", "--exclude", "*"], cwd=here,
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return rev or None
    except (OSError, subprocess.SubprocessError):
        return None


def progress(text: str):
    if sys.stderr.isatty():
        print(f"\r{text:12}", end="\r" if not text else "", file=sys.stderr, flush=True)


def p(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[max(0, math.ceil(q * len(s)) - 1)] if s else 0


def summary(t: dict) -> str:
    n = t["n"] - t["errors"]
    out = [f"action {t['ok']}/{n} right ({100 * t['ok'] / max(n, 1):.0f}%), right kind {t['kind']}"]
    if t["texts"]:
        out.append(f"transcript {t['text_ok']}/{t['texts']} right, WER {100 * t['wer']:.1f}%")
    if t["stt_ms"]:
        out.append(f"speech to text p50 {statistics.median(t['stt_ms']):.0f} p95 {p(t['stt_ms'], .95):.0f} ms")
    if t["decide_ms"]:
        out.append(f"deciding p50 {statistics.median(t['decide_ms']):.0f} p95 {p(t['decide_ms'], .95):.0f} ms")
    out.append(", ".join(f"{k} {v}" for k, v in t["via"].most_common()))
    if t["cost"]:
        out.append(f"${t['cost']:.4f}")
    if t["errors"]:
        out.append(f"{t['errors']} errors")
    return " · ".join(out)


def nothing_to_run(cfg: config.Config):
    print(f"No voice tests yet ({V.LABELS}).")
    recorded = [e for e in activity.read() if e.get("recording") and V.audio(e["recording"])]
    if recorded:
        print(f"{len(recorded)} kept recordings are in the history: run with --seed to label them from what was "
              "decided, or use “Add to voice tests” on one in the panel's Activity tab.")
    elif not cfg.keep_recordings:
        print("Recordings aren't kept: tick “Keep recordings” in the panel's Activity tab, use the TV by voice for a "
              "while, then run this with --seed.")
    else:
        print("No recordings kept yet: give the TV a few commands by voice, then run this with --seed.")


class Hints:
    """The language hint and names the running agent sends a hosted speech model: server.Service's own code, on the
    library as it is now."""
    stt_language = server.Service.stt_language
    stt_vocabulary = server.Service.stt_vocabulary
    _refresh_library_names = server.Service._refresh_library_names
    _fetch_other_names = server.Service._fetch_other_names

    def __init__(self, cfg: config.Config, agent: Agent):
        self.cfg, self.agent = cfg, agent
        self._library_names, self._other_names = {}, {}
        self._library_names_at = time.monotonic()        # read once below, not refreshed during the run


async def run(args, cfg: config.Config, labels: list[dict], specs: list[str]) -> tuple[dict, dict]:
    agent = Agent(cfg, dry_run=True)                     # nothing reaches the TV: only deciding happens
    rules = agent.rules
    if agent.rules_error:
        print(f"rules: {agent.rules_error}; using the defaults", file=sys.stderr)
    taught = [] if args.no_learned else learned.load()
    for x in taught:                                     # as the server does (Service._learn)
        rules.phrases[normalize(x["say"], rules)] = (x["do"], "learned")
    off = {*cfg.decide_off, *filter(None, args.off.split(","))}
    router = Router(agent.d1, rules, agent.llm, agent.llm_context, cfg.decide_steps, off)
    steps = [s for s in cfg.decide_steps if s not in off and (s != "llm" or agent.llm)]     # no key: no LLM
    setup = {"commit": commit(), "version": __version__, "stt_model": cfg.stt_model, "steps": steps,
             "transcripts": args.transcripts,
             "llm_model": cfg.llm_model if "llm" in steps else None,
             "llm_reasoning": cfg.llm_reasoning, "d1_where": cfg.d1_where, "hosted_d1_model": cfg.hosted_d1_model,
             "rules": rules.source, "learned": len(taught)}
    key = cfg.secret("openrouter_api_key")
    language, vocabulary, hints = "", [], None
    if any("/" in s for s in specs):
        hints = Hints(cfg, agent)
        if cfg.stt_vocabulary_library:
            await hints._refresh_library_names()
        language, vocabulary = hints.stt_language(), hints.stt_vocabulary()
        setup |= {"stt_language": language, "stt_vocabulary": len(vocabulary)}

    def hinted(spec: str) -> tuple[str, str, list[str]]:
        """A hosted spec's model, language hint and names: the agent's own, or MODEL@es / @en / @none's."""
        model, at, lang = spec.partition("@")
        if not at:
            return model, language, vocabulary
        lang = "" if lang == "none" else lang
        return model, lang, hints.stt_vocabulary(lang)
    local: dict[str, stt_mod.Stt] = {}
    saved = {}
    if args.transcripts:
        saved = json.loads(place(args.transcripts).read_text()).get("results", {})
    decided: dict[str, object] = {}                      # the same words are decided once per run

    async def hear(spec: str, pcm: bytes, lab: dict) -> dict:
        if spec in ("heard", "text"):
            return {"transcript": lab.get(spec)} if lab.get(spec) is not None else {}
        was = saved.get(lab["recording"], {}).get(spec)
        if was and not was.get("error"):
            return {"transcript": was["transcript"], "stt_ms": was.get("stt_ms")}
        if spec == "local" or spec in stt_mod.MODELS:
            name = cfg.stt_model if spec == "local" else spec
            if name not in local:
                local[name] = await asyncio.to_thread(stt_mod.Stt, stt_mod.model_dir(name))
            t = time.perf_counter()
            text = await asyncio.to_thread(local[name].transcribe, pcm)
            return {"transcript": text, "stt_ms": round((time.perf_counter() - t) * 1000)}
        for _ in range(3):                               # a rate limit or a hiccup: twice more, a second apart
            t = time.perf_counter()
            try:
                model, lang, names = hinted(spec)
                text, cost = await stt_mod.hosted_transcribe(agent.http, key, model, pcm, language=lang,
                                                             vocabulary=names)
                return {"transcript": text, "stt_ms": round((time.perf_counter() - t) * 1000), "stt_cost": cost}
            except (httpx.HTTPError, ValueError, KeyError) as e:
                error = f"{type(e).__name__}"
                if isinstance(e, httpx.HTTPStatusError):        # the status says which: a refused request or theirs
                    error += f" {e.response.status_code}"
                await asyncio.sleep(1)
        return {"transcript": "", "error": error}

    async def decide(text: str) -> tuple:
        """The decision, and how long it took and cost (None for words already decided in this run)."""
        if text in decided:
            return decided[text], None, None
        t = time.perf_counter()
        decided[text] = d = await router.decide(text)
        return d, round((time.perf_counter() - t) * 1000), d.cost

    if "llm" in steps:
        await agent.llm_context("")                      # the library's first fetch isn't a decision's time
    results: dict[str, dict] = {}
    missing = []
    width = max(map(len, specs))
    for i, lab in enumerate(labels):
        name, path = lab["recording"], V.audio(lab["recording"])
        try:
            pcm, seconds = pcm_of(path) if path else (None, 0)
        except (OSError, EOFError, ValueError, wave.Error) as e:
            pcm = None
            print(f"{name}: {str(e) or type(e).__name__}", file=sys.stderr)
        if pcm is None:
            missing.append(name)
            continue
        progress(f"{i + 1}/{len(labels)}")
        lines = []
        for spec in specs:
            r = await hear(spec, pcm, lab)
            if "transcript" not in r:                    # "text" for a label without its words
                continue
            r["expect"] = lab["expect"]
            if lab.get("text") and not r.get("error"):
                r["text_ok"] = V.words(r["transcript"]) == V.words(lab["text"])
                r["wer"] = round(V.wer(lab["text"], r["transcript"]), 3)
            if r.get("error"):
                r.update(ok=False, kind=False, via="error")
            else:
                d, ms, cost = await decide(r["transcript"])
                r["kind"], r["ok"] = judge_label(d.action, lab["expect"], bool(lab.get("exact")))
                r.update(got=d.action.model_dump(exclude_none=True), via=d.via, decide_ms=ms,
                         cost=(cost or 0) + (r.pop("stt_cost", None) or 0) or None)
            results.setdefault(name, {})[spec] = r
            if args.failures and r["ok"] and r.get("text_ok") is not False:
                continue
            mark = "✓" if r["ok"] else "~" if r["kind"] else "✗"
            words_mark = {True: "✓", False: "✗"}.get(r.get("text_ok"), "·")
            got = r["error"] if r.get("error") else r["via"] if r["ok"] else f"{V.describe(r['got'])} · {r['via']}"
            ms = " + ".join(f"{r[k]} ms" for k in ("stt_ms", "decide_ms") if r.get(k) is not None)
            lines.append(f"  {mark} {spec:{width}} {words_mark} “{r['transcript']}” → {got}   {ms}".rstrip())
        if lines:
            said = f" · said “{lab['text']}”" if lab.get("text") else ""
            progress("")
            print(f"{name}  {seconds:.1f} s  should: {V.describe(lab['expect'])}{said}")
            print("\n".join(lines))
    progress("")
    await agent.close()
    if missing:
        print(f"\n{len(missing)} voice tests have no audio (any more) to play: {', '.join(missing[:5])}"
              + (" …" if len(missing) > 5 else ""))
    return results, setup


def show_comparison(before: dict, after: dict) -> int:
    """The changes since a saved run; how many regressions."""
    print(f"\nSince {before.get('saved', '?')}:")
    old_setup, new_setup = before.get("setup", {}), after.get("setup", {})
    for k in sorted(set(old_setup) | set(new_setup)):
        if old_setup.get(k) != new_setup.get(k):
            print(f"  {k}: {old_setup.get(k)} → {new_setup.get(k)}")
    old_t, new_t = V.totals(before.get("results", {})), V.totals(after.get("results", {}))
    for model in sorted(set(old_t) | set(new_t)):
        a, b = old_t.get(model), new_t.get(model)
        if a and b:
            text = (f", transcript {a['text_ok']}/{a['texts']} → {b['text_ok']}/{b['texts']}"
                    if a["texts"] or b["texts"] else "")
            print(f"  {model}: action right {a['ok']}/{a['n']} → {b['ok']}/{b['n']}{text}")
    c = V.compare(before, after)
    if not any(c[k] for k in ("regressions", "fixes", "changed")):
        print("  No regressions, fixes or changed transcripts.")

    def item(x: dict) -> str:
        a, b = x["before"], x["after"]
        if x.get("what") == "transcript" or "got" not in a or "got" not in b or a["got"] == b["got"]:
            return f"“{a.get('transcript', '')}” → “{b.get('transcript', '')}”"
        return (f"{V.describe(a['got'])} → {V.describe(b['got'])}"
                + (f"  (“{a['transcript']}” → “{b['transcript']}”)" if a.get("transcript") != b.get("transcript")
                   else f"  (“{b['transcript']}”, {a.get('via')} → {b.get('via')})"))

    for key, title in (("regressions", "Regressions"), ("fixes", "Fixes"), ("relabelled", "Label changed"),
                       ("changed", "Other transcripts that changed"), ("errors", "A model failed (not compared)")):
        if c[key]:
            print(f"\n{title} ({len(c[key])}):")
            for x in c[key]:
                what = f" {x['what']}:" if x.get("what") else ":"
                print(f"  {x['recording']} {x['stt']}{what} {item(x)}")
    for key, title in (("only_before", "Only in the saved run"), ("only_after", "New in this run")):
        if c[key]:
            print(f"\n{title}: {len(c[key])} ({', '.join(f'{n} {m}' for n, m in c[key][:4])}"
                  + (" …)" if len(c[key]) > 4 else ")"))
    return len(c["regressions"])


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--seed", action="store_true", help="label the kept recordings from the history first")
    ap.add_argument("--stt", default="local", help="comma-separated: local, parakeet-v2, parakeet-v3, heard, text "
                                                   "or OpenRouter transcription model ids")
    ap.add_argument("--off", default="", help=f"decision steps to switch off too ({', '.join(STEPS)})")
    ap.add_argument("--no-learned", action="store_true", help="without the sentences taught in the panel")
    ap.add_argument("--failures", action="store_true", help="show only what's wrong (and the totals)")
    ap.add_argument("--save", help="keep this run's results (a bare name: with the voice tests)")
    ap.add_argument("--compare", help="show regressions and fixes since a saved run")
    ap.add_argument("--transcripts", help="reuse a saved run's transcripts: the decision steps only")
    args = ap.parse_args()
    cfg = config.load()
    specs = [s.strip() for s in args.stt.split(",") if s.strip()]
    if bad := set(filter(None, args.off.split(","))) - set(STEPS):
        ap.error(f"--off takes {', '.join(STEPS)}, not {', '.join(sorted(bad))}")
    for spec in specs:
        name = cfg.stt_model if spec == "local" else spec
        if "@" in spec and ("/" not in spec or not re.fullmatch(r"[a-z]{2}|none", spec.partition("@")[2])):
            ap.error(f"--stt {spec}: a hint goes after an OpenRouter model id: @es, @en, @none")
        if (spec == "local" or spec in stt_mod.MODELS) and not stt_mod.installed(name):
            ap.error(f"the speech model {name} isn't installed (tv-agent setup stt {name})")
        if "/" in spec and not cfg.secret("openrouter_api_key"):
            ap.error(f"{spec} needs an OpenRouter key (tv-agent setup openrouter)")
        if spec not in ("local", "heard", "text") and spec not in stt_mod.MODELS and "/" not in spec:
            ap.error(f"--stt {spec}: local, parakeet-v2, parakeet-v3, heard, text or an OpenRouter model id")
    before = None
    if args.compare:
        try:
            before = json.loads(place(args.compare).read_text())
        except (OSError, ValueError) as e:
            ap.error(f"--compare: {e}")

    try:
        if args.seed:
            done = V.seed_history()
            added = ", ".join(f"{done.pop(k)} {k}" for k in ("added", "updated", "removed") if done.get(k))
            already = done.pop("labelled", 0)
            print(f"Seeded from the history: {added or 'nothing new'}"
                  + (f"; {already} already labelled" if already else "")
                  + (f"; not labelled: {', '.join(f'{v} {k}' for k, v in done.most_common())}" if done else ""))
        labels = V.load()
    except ValueError as e:                              # a label file edited into something else
        print(e, file=sys.stderr)
        return 2
    if not labels:
        nothing_to_run(cfg)
        return 0

    results, setup = await run(args, cfg, labels, specs)
    after = {"saved": datetime.now().isoformat(timespec="seconds"), "setup": setup, "stt": specs, "results": results}
    print(f"\n{len(labels)} voice tests, decided with {', '.join(setup['steps']) or 'nothing'}"
          f"{' (and what you taught)' if setup['learned'] else ''}:")
    for model, t in V.totals(results).items():
        label = f"{model} ({cfg.stt_model})" if model == "local" else model
        print(f"  {label}\n    {summary(t)}")
    regressions = show_comparison(before, after) if before else 0
    if args.save:
        out = place(args.save)
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # the transcripts are private
        with os.fdopen(fd, "w") as f:
            json.dump(after, f, ensure_ascii=False, indent=1)
        print(f"\nSaved to {out}")
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
