"""tv-agent command line.

    tv-agent serve                                              the daemon (run by the tv-agent user service)
    tv-agent say "pause" [--dry-run]                            decide and run one sentence
    tv-agent do '{"type":"open_app","app":"kodi"}' [--dry-run]   run one action
    tv-agent doctor                                             check the TV, models, secrets
    tv-agent eval [-v]                                          score the rules on eval/utterances.jsonl
    tv-agent setup token|stremio|kodi|services                  one-time setup steps
    tv-agent tv host|shell|install|pair                         the TV: find it, run a command, set up its app
    tv-agent secret agent_token                                 print one secret
"""
import argparse
import asyncio
import json
import sys

import httpx

from . import __version__, config
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


async def _setup_kodi() -> int:
    """Asks for the password you chose in Kodi (Settings > Services > Control), checks it, saves it."""
    import getpass
    from .tv.kodi import Kodi, KodiError
    cfg = config.load()
    password = getpass.getpass(f"Kodi web password for user '{cfg.kodi_user}': ")
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
            print(f"[{s.score:7.1f}] {s.filename or s.name!r} {s.why_dropped}")
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


async def _eval(verbose: bool) -> int:
    """Run every labelled sentence through the router (nothing is sent to the TV); report accuracy and latency."""
    from .decide import evaluate
    from .decide import rules as R
    from .decide.router import Router
    cfg = config.load()
    rules, err = R.load_safe()
    if err:
        print(f"warning: {err}; scoring the defaults")
    async with httpx.AsyncClient() as http:
        res = await evaluate.run(Router(D1(http, cfg.d1_url), rules))
    for r in res["results"]:
        if verbose or not r["ok"]:
            print(f"{'ok ' if r['ok'] else 'BAD'} {r['text']!r:52} -> {r['via']:7} {r['rule'][:18]:18} "
                  f"{r['confidence']:.2f} {json.dumps(r['got'])}")
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
        d = await Router(agent.d1, agent.rules).decide(text)
        out = await agent.execute(d.action)
        print(json.dumps({"heard": text, "via": d.via, "rule": d.rule, "confidence": round(d.confidence, 2),
                          "action": d.action.model_dump(), "ok": out.ok, "say": out.say}, ensure_ascii=False))
        return 0 if out.ok else 1
    finally:
        await agent.close()


def _tv(what: str, rest: list[str]) -> int:
    from .install import install_app, pair
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
            if len(rest) != 1:
                print("usage: tv-agent tv install <app.apk>", file=sys.stderr)
                return 2
            from pathlib import Path
            url = install_app(cfg, adb, Path(rest[0]))
            print(f"installed and paired with {url}")
        elif what == "pair":
            print(f"the TV app now talks to {pair(cfg, adb)}")
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
    check("d1-3B", lambda: httpx.get(f"{cfg.d1_url}/v1/models", timeout=3).json()["data"][0]["id"])
    check("speech model", lambda: cfg.stt_model.name if (cfg.stt_model / "tokens.txt").exists() else 1 / 0)
    for name in config.SECRET_NAMES:
        check(f"secret {name}", lambda name=name: "set" if cfg.secret(name) else "missing (see README setup)")
    agent.adb.close()
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="tv-agent", description="Voice decision agent for the living-room TV")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("do", help="run one action given as JSON")
    d.add_argument("action")
    d.add_argument("--dry-run", action="store_true", help="print the TV commands instead of sending them")
    sub.add_parser("doctor", help="check the TV connection, models and secrets")
    sub.add_parser("serve", help="run the daemon the TV app talks to")
    e = sub.add_parser("eval", help="score the router on eval/utterances.jsonl (sends nothing to the TV)")
    e.add_argument("-v", "--verbose", action="store_true")
    sy = sub.add_parser("say", help="decide and run one sentence, as if spoken")
    sy.add_argument("text")
    sy.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("setup", help="one-time setup steps")
    s.add_argument("what", choices=["token", "stremio", "kodi", "services"])
    s.add_argument("--rotate", action="store_true", help="token: replace it (then run `tv-agent tv pair`)")
    s.add_argument("--enable", action="store_true", help="services: also enable and start them")
    tv = sub.add_parser("tv", help="the TV over ADB: find it, run a command, install and pair its app")
    tv.add_argument("what", choices=["host", "shell", "install", "pair"])
    tv.add_argument("args", nargs=argparse.REMAINDER, help="shell: the command and its arguments; install: the APK")
    st = sub.add_parser("streams", help="list the streams your addons offer for a title (names only, no links)")
    st.add_argument("title")
    st.add_argument("--season", type=int)
    st.add_argument("--episode", type=int)
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
        return asyncio.run(_eval(args.verbose))
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
        print("enabled and started" if args.enable else
              "start them with: systemctl --user enable --now llama-d1.service tv-agent.service")
        return 0
    if args.cmd == "tv":
        return _tv(args.what, args.args)
    if args.cmd == "setup" and args.what == "stremio":
        return asyncio.run(_setup_stremio())
    if args.cmd == "setup" and args.what == "kodi":
        return asyncio.run(_setup_kodi())
    if args.cmd == "streams":
        return asyncio.run(_streams(args.title, args.season, args.episode))
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
