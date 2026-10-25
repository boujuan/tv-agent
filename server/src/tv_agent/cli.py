"""tv-agent command line.

    tv-agent serve                                              the daemon (run by the tv-agent user service)
    tv-agent say "pause" [--dry-run]                            decide and run one sentence
    tv-agent do '{"type":"open_app","app":"kodi"}' [--dry-run]   run one action
    tv-agent doctor                                             check the TV, models, secrets
    tv-agent eval [-v]                                          score the rules on eval/utterances.jsonl
    tv-agent setup token|stremio|kodi|services|nightly          one-time setup steps
    tv-agent quality nightly [--only voice,chains,finding]      the nightly check now (quality.py)
    tv-agent tv host|shell|install|pair                         the TV: find it, run a command, set up its app
    tv-agent secret agent_token                                 print one secret
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

from . import __version__, config, stt
from .actions import parse
from .agent import Agent
from .decide.d1 import D1
from .redact import remember, setup_logging


def _agent(dry_run=False) -> Agent:
    cfg = config.load()
    remember(*cfg.secrets.values())
    return Agent(cfg, dry_run=dry_run)


async def _do(text: str, dry_run: bool):
    action = parse(text)
    agent = _agent(dry_run)
    try:
        out = await agent.execute(action)
        print(json.dumps({"ok": out.ok, "say": out.say, "action": action.model_dump()}, ensure_ascii=False))
        return 0 if out.ok else 1
    finally:
        await agent.close()


async def _setup_stremio() -> int:
    from .stremio.account import Account, link_login
    async with httpx.AsyncClient(headers={"User-Agent": f"tv-agent/{__version__}"}) as http:
        def show(link):
            print(f"Open this link in a browser where you're signed in to Stremio and approve it:\n  {link}",
                  flush=True)
        key = await link_login(http, show)
        remember(key)
        config.save_secret("stremio_auth_key", key)
        addons = await Account(http, key).addons()
        print(f"Signed in. Auth key saved in {config.SECRETS_FILE}. Addons: {', '.join(a.name for a in addons)}")
    return 0


KEY_CHECKS = {
    # what to call, and what to show from the answer (never the key)
    "liquid": ("Liquid API key", "liquid_api_key", "https://api.liquid.ai/decisions/v1/models",
               lambda j: "models: " + ", ".join(m.get("id", "?") for m in j.get("data", []))),
    "openrouter": ("OpenRouter API key", "openrouter_api_key", "https://openrouter.ai/api/v1/key",
                   lambda j: f"key “{(j.get('data') or {}).get('label') or 'unnamed'}”"),
}


def _setup_key(what: str) -> int:
    """Saves a cloud fallback's API key after checking it works. Piped in (`op read "op://…" | tv-agent setup
    liquid`) or typed at a hidden prompt; it's never shown or logged."""
    import getpass
    label, name, url, describe = KEY_CHECKS[what]
    key = sys.stdin.read().strip() if not sys.stdin.isatty() else getpass.getpass(f"{label}: ").strip()
    if not key or any(c.isspace() for c in key):
        print(f"No {label} given; nothing saved")
        return 1
    remember(key)
    try:
        r = httpx.get(url, headers={"Authorization": f"Bearer {key}", "User-Agent": f"tv-agent/{__version__}"},
                      timeout=15)
    except httpx.HTTPError as e:
        print(f"Not saved: couldn't reach {httpx.URL(url).host} ({type(e).__name__})")
        return 1
    if r.status_code != 200:
        print(f"Not saved: {httpx.URL(url).host} answered {r.status_code} (is the key right?)")
        return 1
    config.save_secret(name, key)
    print(f"{label} saved in {config.SECRETS_FILE} ({describe(r.json())}). Restart the voice agent to use it: "
          "systemctl --user restart tv-agent")
    return 0


async def _review(n: int) -> int:
    """The history's commands marked wrong or not understood, newest first; with an OpenRouter key, the LLM's guess
    at what was meant. Teach one in the panel (Activity: right-click, Teach the right action…)."""
    from . import activity
    from .decide import rules as R
    from .decide.llm import Llm
    cfg = config.load()
    rows = [e for e in activity.read(limit=1000)
            if (e.get("wrong") or e.get("type") == "none") and e.get("heard", "").strip()][:n]
    if not rows:
        print("Nothing marked wrong or left not understood.")
        return 0
    key = cfg.secret("openrouter_api_key")
    async with httpx.AsyncClient(headers={"User-Agent": f"tv-agent/{__version__}"}) as http:
        llm = Llm(http, key, cfg.llm_model, R.load_safe()[0]) if key else None
        for e in rows:
            when = time.strftime("%d %b %H:%M", time.localtime(e["t"]))
            why = "marked wrong" if e.get("wrong") else "not understood"
            taught = f", taught: {e['taught']}" if e.get("taught") else ""
            line = f"{when}  “{e.get('heard', '')}” -> {e.get('type')} ({why}{taught})"
            if llm and not e.get("taught"):
                guess = await llm.decide(e.get("heard", ""), {})
                line += f"\n               LLM's guess: {guess.model_dump(exclude_none=True) if guess else 'not a command'}"
            print(line)
    print("Teach one in the panel: Activity, right-click it, “Teach the right action…”.")
    return 0


def _setup_stt(name: str | None) -> int:
    """Without a name: the speech models and which is in use. With one: download it if needed, then use it."""
    cfg = config.load()
    if not name:
        for key, m in stt.MODELS.items():
            state = "in use" if key == cfg.stt_model else "downloaded" if stt.installed(key) else \
                f"{m.size / 1e6:.0f} MB download"
            print(f"{key:12} {m.label} · {m.languages} · {state}")
        return 0
    if name not in stt.MODELS:
        print(f"unknown speech model {name!r}; choose from {', '.join(stt.MODELS)}")
        return 2
    if not stt.installed(name):
        m = stt.MODELS[name]
        print(f"Downloading {m.archive}.tar.bz2 ({m.size / 1e6:.0f} MB) from GitHub (k2-fsa/sherpa-onnx)…", flush=True)
        shown = [-1]

        def progress(done, total):
            pct = done * 100 // max(total, 1)
            if pct >= shown[0] + 10:
                shown[0] = pct
                print(f"  {pct}%", flush=True)
        with httpx.Client(headers={"User-Agent": f"tv-agent/{__version__}"}) as http:
            print(f"Installed in {stt.download(name, http, progress)}")
    config.set_value(cfg, "stt_model", name)
    print(f"Using {name}. Restart the voice agent to load it: systemctl --user restart tv-agent")
    return 0


async def _setup_kodi() -> int:
    """Asks for the password you chose in Kodi (Settings > Services > Control), checks it, saves it."""
    import getpass
    from .tv.kodi import Kodi, KodiError
    cfg = config.load()
    password = (sys.stdin.read().rstrip("\r\n") if not sys.stdin.isatty()        # piped (the panel, a password manager)
                else getpass.getpass(f"Kodi web password for user '{cfg.kodi_user}': "))
    agent = Agent(cfg)
    kodi = Kodi(agent.http, agent.adb, cfg.kodi_port, cfg.kodi_user, password)
    try:
        await kodi.start()
        version = await kodi.call("JSONRPC.Version")
        methods = (await kodi.call("JSONRPC.Introspect", {"getdescriptions": False}))["methods"]
    except KodiError as e:
        print(f"Not saved: {e}")
        return 1
    finally:
        await agent.close()
    config.save_secret("kodi_password", password)
    v = version["version"]
    print(f"Kodi JSON-RPC {v['major']}.{v['minor']} answers; password saved. "
          f"Player.AddSubtitle: {'yes' if 'Player.AddSubtitle' in methods else 'no'}")
    return 0


async def _streams(title, season, episode) -> int:
    """Show what the play pipeline would choose from: addon, name and description of each stream, no URLs."""
    agent = _agent()
    try:
        m = await agent.find_title(title, None, season)
        if not m:
            print("not found")
            return 1
        video = await agent.episode_id(m, season, episode) or m.id
        from .stremio import cinemeta
        meta = await cinemeta.meta(agent.http, m.type, m.id)
        print(f"{m.label} -> {video} ({meta.get('country')}; tracks {agent.track_plan(meta)})")
        for s in await agent.streams_for(m.type, video, meta):
            rate = s.mbps(None)
            facts = " ".join(x for x in (f"{s.height}p" if s.height else "", s.quality, "/".join(s.visual),
                                         "/".join(s.languages), f"{rate:.0f} Mbit/s" if rate else "",
                                         "library" if s.library else ("cached" if s.cached else "")) if x)
            print(f"[{s.score:7.1f}] {s.filename or s.name!r}  {facts}  {s.why_dropped}")
        return 0
    finally:
        await agent.close()


async def _logout() -> int:
    from .stremio.account import Account
    cfg = config.load()
    key = cfg.secret("stremio_auth_key")
    if key:
        async with httpx.AsyncClient() as http:
            await Account(http, key).logout()
        config.save_secret("stremio_auth_key", None)
    print("Signed out of Stremio; auth key removed")
    return 0


async def _eval(verbose: bool, file: str | None, split: str | None) -> int:
    """Run labelled sentences through the router (nothing is sent to the TV); report accuracy and latency."""
    from .decide import evaluate
    from .decide import rules as R
    from .decide.router import Router
    cfg = config.load()
    rules, err = R.load_safe()
    if err:
        print(f"warning: {err}; scoring the defaults")
    async with httpx.AsyncClient() as http:
        path = Path(file) if file else evaluate.EVAL_FILE
        d1 = D1(http, cfg.d1_url, cfg.secret("liquid_api_key"), cfg.hosted_d1_model, cfg.d1_where,   # as configured
                cfg.secret("openrouter_api_key"))
        res = await evaluate.run(Router(d1, rules), path, split)
    for r in res["results"]:
        if verbose or not r["ok"]:
            print(f"{'ok ' if r['ok'] else 'BAD'} {r['text']!r:52} -> {r['via']:7} {r['rule'][:18]:18} "
                  f"{r['confidence']:.2f} {json.dumps(r['got'])}")
    cats = {}
    for r in res["results"]:
        if r["cat"]:
            c = cats.setdefault(r["cat"], [0, 0])
            c[0] += r["ok"]
            c[1] += 1
    if cats:
        print("  ".join(f"{c} {a}/{b}" for c, (a, b) in sorted(cats.items(), key=lambda kv: kv[1][0] / kv[1][1])))
    n = res["n"]
    print(f"\nintent {res['intent_ok']}/{n} ({100 * res['intent_ok'] / n:.0f}%), "
          f"full action {res['full_ok']}/{n} ({100 * res['full_ok'] / n:.0f}%)")
    for via, v in sorted(res["timing"].items()):
        print(f"  {via:8} n={v['n']:3} p50 {v['p50']:6.1f} ms  p95 {v['p95']:6.1f} ms")
    return 0 if res["intent_ok"] / n >= 0.95 and res["full_ok"] / n >= 0.90 else 1


async def _say(text: str, dry_run: bool) -> int:
    from .decide.router import Router
    agent = _agent(dry_run)
    try:
        d = await Router(agent.d1, agent.rules, agent.llm, agent.llm_context, agent.cfg.decide_steps,
                         agent.cfg.decide_off).decide(text)
        out = await agent.execute(d.action)
        print(json.dumps({"heard": text, "via": d.via, "rule": d.rule, "confidence": round(d.confidence, 2),
                          "action": d.action.model_dump(), "ok": out.ok, "say": out.say}, ensure_ascii=False))
        return 0 if out.ok else 1
    finally:
        await agent.close()


def _tv(what: str, rest: list[str]) -> int:
    from .install import PLAYER, install_app, installed, pair, pair_player
    from .tv.adb import Adb
    cfg = config.load()
    adb = Adb(cfg)
    try:
        if what == "host":
            adb.run(["true"])
            print(adb.host)
        elif what == "shell":
            if not rest:
                print("usage: tv-agent tv shell <command> [args…]", file=sys.stderr)
                return 2
            print(adb.run(rest, timeout=60), end="")
        elif what == "install":
            from pathlib import Path
            if len(rest) != 1 or not Path(rest[0]).is_file():     # "--help" too: it arrives here as a file name
                print("usage: tv-agent tv install <app.apk>", file=sys.stderr)
                return 2
            url = install_app(cfg, adb, Path(rest[0]))
            print(f"installed and paired with {url}")
        elif what == "pair":
            print(f"the TV app now talks to {pair(cfg, adb)}")
            if installed(adb, PLAYER):
                print(f"TV Agent Player now talks to {pair_player(cfg, adb)}")
        return 0
    finally:
        adb.close()


def _doctor() -> int:
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            print(f"ok    {name}: {fn()}")
        except BaseException as e:   # SystemExit from the permission checks too
            ok = False
            print(f"FAIL  {name}: {e}")

    check("config permissions", lambda: (config.check_permissions(), "private")[1])
    cfg = config.load()
    agent = Agent(cfg)
    check("ADB", lambda: f"{agent.adb.run(['getprop', 'ro.product.model']).strip()} at {agent.adb.host}")
    steps = " → ".join(s + (" (off)" if s in cfg.decide_off else "") for s in cfg.decide_steps)
    print(f"ok    decision steps: {steps}; d1 {cfg.d1_where}; LLM {cfg.llm_model}")
    if "d1" in cfg.decide_off or cfg.d1_where == "hosted":
        print("ok    local d1-3B: not used by the decision steps")
    else:
        check("local d1-3B", lambda: httpx.get(f"{cfg.d1_url}/v1/models", timeout=3).json()["data"][0]["id"])
    via_openrouter = "/" in cfg.hosted_d1_model
    if "d1" not in cfg.decide_off and cfg.d1_where != "local" and not cfg.secret(
            "openrouter_api_key" if via_openrouter else "liquid_api_key"):
        ok = False
        print(f"FAIL  hosted decision model {cfg.hosted_d1_model}: no key (tv-agent setup "
              f"{'openrouter' if via_openrouter else 'liquid'})")
    if "llm" not in cfg.decide_off and not cfg.secret("openrouter_api_key"):
        print("ok    LLM step: no OpenRouter key, so it's skipped (optional: tv-agent setup openrouter)")
    def speech():
        if not stt.installed(cfg.stt_model):
            raise RuntimeError(f"{cfg.stt_model} isn't downloaded (tv-agent setup stt {cfg.stt_model})")
        return cfg.stt_model
    check("speech model", speech)
    for name in config.SECRET_NAMES:
        if name in config.OPTIONAL_SECRETS:
            what = name.removesuffix("_api_key")
            print(f"ok    secret {name}: {'set' if cfg.secret(name) else f'not set (optional: tv-agent setup {what})'}")
            continue
        if name == "kodi_password" and cfg.player != "kodi" and not cfg.secret(name):
            print(f"ok    secret {name}: not needed (the player is {cfg.player})")
            continue
        check(f"secret {name}", lambda name=name: "set" if cfg.secret(name) else "missing (see README setup)")
    agent.adb.close()
    return 0 if ok else 1


def _nightly(only: list[str]) -> int:
    """The nightly check, with the agent's own speech to text (its hosted model, else this laptop's): exit 1 on a
    regression."""
    from . import quality
    cfg = config.load()
    stt_spec = cfg.stt_hosted_model if cfg.stt_where.startswith("hosted") else "local"
    run = quality.nightly(stt_spec, only or None)
    for name, b in run["benches"].items():
        if b.get("error"):
            print(f"{quality.BENCHES[name]}: {b['error']}")
            continue
        print(f"{quality.BENCHES[name]}: {b['right']}/{b['of']} right, {len(b['regressions'])} regressions, "
              f"{len(b['fixes'])} fixed, ${b['cost']:.4f}, {b['seconds']} s"
              + (f"; right again when rerun: {len(b['unsteady'])}" if b.get("unsteady") else "")
              + (f"; couldn't be checked: {len(b['unavailable'])}" if b.get("unavailable") else ""))
        for k in b["regressions"]:
            print(f"  ✗ {k}")
    print(f"cost ${run['cost']:.4f}")
    return 1 if run["regressions"] else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="tv-agent", description="Voice decision agent for the living-room TV")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("do", help="run one action given as JSON")
    d.add_argument("action")
    d.add_argument("--dry-run", action="store_true", help="print the TV commands instead of sending them")
    sub.add_parser("doctor", help="check the TV connection, models and secrets")
    sub.add_parser("serve", help="run the daemon the TV app talks to")
    e = sub.add_parser("eval", help="score the router on labelled sentences (sends nothing to the TV)")
    e.add_argument("-v", "--verbose", action="store_true")
    e.add_argument("--file", help="a .jsonl of labelled sentences (default eval/utterances.jsonl; "
                                  "the big set is eval/commands.jsonl)")
    e.add_argument("--split", choices=("dev", "test"), help="only that part of a file that has one")
    sy = sub.add_parser("say", help="decide and run one sentence, as if spoken")
    sy.add_argument("text")
    sy.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("setup", help="one-time setup steps")
    s.add_argument("what", choices=["token", "stremio", "kodi", "services", "nightly", "stt", "liquid", "openrouter"])
    s.add_argument("name", nargs="?", help="stt: the speech model to download (if needed) and use")
    s.add_argument("--rotate", action="store_true", help="token: replace it (then run `tv-agent tv pair`)")
    s.add_argument("--enable", action="store_true", help="services: also enable and start them")
    q = sub.add_parser("quality", help="the nightly check: voice tests, sentences, episode and scene searches")
    q.add_argument("what", choices=["nightly"])
    q.add_argument("--only", default="", help="some of: voice, chains, finding")
    tv = sub.add_parser("tv", help="the TV over ADB: find it, run a command, install and pair its app")
    tv.add_argument("what", choices=["host", "shell", "install", "pair"])
    tv.add_argument("args", nargs=argparse.REMAINDER,
                    help="shell: the command and its arguments; install: the APK (the TV app or TV Agent Player)")
    st = sub.add_parser("streams", help="list the streams your addons offer for a title (names only, no links)")
    st.add_argument("title")
    st.add_argument("--season", type=int)
    st.add_argument("--episode", type=int)
    rv = sub.add_parser("review", help="commands marked “that's wrong” or not understood (with an LLM's guess)")
    rv.add_argument("-n", type=int, default=20, help="how many (newest first)")
    sub.add_parser("logout", help="sign the agent out of Stremio (invalidates its auth key)")
    sec = sub.add_parser("secret", help="print one secret (for build scripts)")
    sec.add_argument("name", choices=["agent_token"])
    args = p.parse_args(argv)
    setup_logging()

    if args.cmd == "do":
        return asyncio.run(_do(args.action, args.dry_run))
    if args.cmd == "doctor":
        return _doctor()
    if args.cmd == "serve":
        from .server import serve
        cfg = config.load()
        remember(*cfg.secrets.values())
        asyncio.run(serve(cfg))
        return 0
    if args.cmd == "eval":
        return asyncio.run(_eval(args.verbose, args.file, args.split))
    if args.cmd == "say":
        return asyncio.run(_say(args.text, args.dry_run))
    if args.cmd == "setup" and args.what == "token":
        cfg = config.load()
        if cfg.secret("agent_token") and not args.rotate:
            print("agent token already set (use --rotate to replace it, then `tv-agent tv pair`)")
            return 0
        config.save_secret("agent_token", config.new_token())
        print(f"agent token saved in {config.SECRETS_FILE}; run `tv-agent tv pair` so the TV app uses it")
        return 0
    if args.cmd == "setup" and args.what == "services":
        from .install import write_services
        for path in write_services(config.load(), args.enable):
            print(f"wrote {path}")
        print("enabled and started (llama-d1 only when the decision steps use the local d1)" if args.enable else
              "start them with: systemctl --user enable --now tv-agent.service, and llama-d1.service for the local d1")
        return 0
    if args.cmd == "tv":
        return _tv(args.what, args.args)
    if args.cmd == "setup" and args.what == "nightly":
        from .install import write_nightly
        for path in write_nightly(getattr(args, "enable", False)):
            print(f"wrote {path}")
        return 0
    if args.cmd == "quality":
        return _nightly([x for x in args.only.split(",") if x])
    if args.cmd == "setup" and args.what == "stt":
        return _setup_stt(args.name)
    if args.cmd == "setup" and args.what in ("liquid", "openrouter"):
        return _setup_key(args.what)
    if args.cmd == "setup" and args.what == "stremio":
        return asyncio.run(_setup_stremio())
    if args.cmd == "setup" and args.what == "kodi":
        return asyncio.run(_setup_kodi())
    if args.cmd == "streams":
        return asyncio.run(_streams(args.title, args.season, args.episode))
    if args.cmd == "review":
        return asyncio.run(_review(args.n))
    if args.cmd == "logout":
        return asyncio.run(_logout())
    if args.cmd == "secret":
        value = config.load().secret(args.name)
        if not value:
            print(f"{args.name} is not set", file=sys.stderr)
            return 1
        print(value)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
