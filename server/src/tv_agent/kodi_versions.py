"""Kodi on the TV kept up to date from the panel: the newest Kodi on Kodi's mirrors for the TV's ABI (Kodi 22 Piers
is a release candidate for now, with MediaTek fixes this TV's decoders need: seeking, xbmc PRs #28505, #28839). An
update first backs up Kodi's settings, libraries and add-ons on the TV (/sdcard/tv-agent/kodi-<version>.tar, ~18 MB,
outside Kodi's folder so an uninstall keeps it), since Kodi 22 converts its databases one way and Android installs
no older version over a newer one: going back to 21 (only through the API) uninstalls 22, installs 21 and restores
that backup. The APK is installed only with the SHA-256 the mirror publishes for it. An update runs in the
background (a minute or two: a 70 MB download, the install, Kodi's first start).

    GET  /v1/kodi                   installed, the newest build (and of each version), backups, the update running
    POST /v1/kodi/update {"replace": bool}       to the newest build (the panel only)
    POST /v1/kodi/switch {"to": "21"|"22", "replace": bool}   to a version, e.g. back to 21 (the panel only)
"""
import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import config as config_mod

log = logging.getLogger("tv_agent")
PACKAGE = "org.xbmc.kodi"
MIRROR = "https://mirrors.kodi.tv/releases/android/{dir}/"
ABI_DIRS = {"armeabi-v7a": "arm", "arm64-v8a": "arm64-v8a", "x86_64": "x86_64"}
NAMES = {"21": "Omega", "22": "Piers"}
DATA = "/sdcard/Android/data/org.xbmc.kodi/files"
BACKUPS = "/sdcard/tv-agent"
APK = re.compile(r"kodi-(\d+)\.(\d+)-(\w+?)(?:_(alpha|beta|rc)(\d+))?-([\w-]+)\.apk$")
STAGE = {"alpha": 0, "beta": 1, "rc": 2, None: 3}


def parse(name: str) -> dict | None:
    m = APK.match(name)
    if not m:
        return None
    major, minor, codename, stage, n, abi = m.groups()
    label = f"{major}.{minor}" + (f" {stage.upper() if stage == 'rc' else stage.capitalize()} {n}" if stage else "")
    return {"file": name, "major": major, "version": f"{major}.{minor}", "label": f"{label} ({codename})", "abi": abi,
            "stable": stage is None, "key": (int(major), int(minor), STAGE[stage], int(n or 0))}


def version_key(name: str | None) -> tuple:
    """Kodi's own version name ("21.2", "22.0-RC1") as parse()'s key, to compare with the mirror's builds."""
    m = re.match(r"(\d+)\.(\d+)(?:-(alpha|beta|rc)(\d+))?", (name or "").lower())
    return (int(m.group(1)), int(m.group(2)), STAGE[m.group(3)], int(m.group(4) or 0)) if m else ()


def newest(listing: str, abi: str) -> dict[str, dict]:
    """The newest build of each major version on a mirror's directory page (any stage: Kodi 22 is a release
    candidate for now), for this ABI."""
    best: dict[str, dict] = {}
    for name in set(re.findall(r'href="([^"]+\.apk)"', listing)):
        p = parse(name)
        if p and p["abi"] == abi and p["major"] in NAMES and (p["major"] not in best or p["key"] > best[p["major"]]["key"]):
            best[p["major"]] = p
    return best


def installed(adb) -> dict:
    """Kodi on the TV (version, ABI, where it came from) and the backups made before switches."""
    out = adb.run(["sh", "-c", f"dumpsys package {PACKAGE} | grep -m4 -E 'versionName|versionCode|primaryCpuAbi|"
                               f"installerPackageName'; echo ABIS $(getprop ro.product.cpu.abilist); "
                               f"for f in {BACKUPS}/kodi-*.tar; do [ -f $f ] && echo BACKUP $f $(stat -c '%s %Y' $f); "
                               f"done"], timeout=15)
    name = re.search(r"versionName=(\S+)", out)
    abi = re.search(r"primaryCpuAbi=([\w-]+)", out)
    abis = re.search(r"^ABIS ([\w,-]+)", out, re.M)
    return {"installed": bool(name), "version": name.group(1) if name else None,
            "code": int(m.group(1)) if (m := re.search(r"versionCode=(\d+)", out)) else None,
            "abi": abi.group(1) if abi and abi.group(1) != "null" else
            (abis.group(1).split(",")[0] if abis else "armeabi-v7a"),
            "from": m.group(1) if (m := re.search(r"installerPackageName=(\S+)", out)) else None,
            "backups": {m.group(1): {"size": int(m.group(2)), "made": int(m.group(3))}
                        for m in re.finditer(r"^BACKUP \S+/kodi-([\d.]+)\.tar (\d+) (\d+)", out, re.M)}}


class KodiVersions:
    def __init__(self, svc):
        self.svc = svc
        self.job: dict | None = None            # {"to", "step", "done", "error", "needs"}
        self._available: tuple[float, dict] | None = None

    @property
    def adb(self):
        return self.svc.agent.adb

    async def available(self, abi: str) -> dict[str, dict]:
        if self._available and time.time() - self._available[0] < 3600:
            return self._available[1]
        r = await self.svc.agent.http.get(MIRROR.format(dir=ABI_DIRS.get(abi, "arm")), timeout=20)
        r.raise_for_status()
        found = {k: {x: v[x] for x in ("file", "version", "label", "stable")} for k, v in newest(r.text, abi).items()}
        self._available = (time.time(), found)
        return found

    async def state(self) -> dict:
        try:
            now = await asyncio.to_thread(installed, self.adb)
        except Exception as e:
            return {"error": f"the TV: {type(e).__name__}", "job": self.job}
        try:
            avail = await self.available(now["abi"])
        except (httpx.HTTPError, ValueError) as e:
            avail, now["mirror_error"] = {}, type(e).__name__
        top = max(avail.items(), key=lambda kv: parse(kv[1]["file"])["key"], default=(None, None))
        return {**now, "major": (now["version"] or "").split(".")[0] or None, "available": avail, "job": self.job,
                "latest": top[1] and {**top[1], "major": top[0]},
                "newer": bool(top[1] and now["version"] and parse(top[1]["file"])["key"] > version_key(now["version"]))}

    def _step(self, text: str):
        log.info("Kodi switch: %s", text)
        self.job["step"] = text

    async def switch(self, to: str, replace: bool = False):
        """Runs as the background job (self.job tells how far it got)."""
        self.job = {"to": to, "step": "starting", "done": False, "error": None, "needs": None, "started": time.time()}
        apk = None
        try:
            now = await asyncio.to_thread(installed, self.adb)
            avail = await self.available(now["abi"])
            if to not in avail:
                raise RuntimeError(f"no Kodi {to} build for {now['abi']} on Kodi's mirrors")
            target = avail[to]
            cur = (now["version"] or "").split(".")[0]
            downgrade = bool(cur) and int(to) < int(cur)
            if downgrade and cur_backup_missing(now, to):
                raise RuntimeError(f"no backup from Kodi {to} on the TV to restore: going back would start Kodi empty")
            self._step(f"downloading {target['file']}")
            apk = await self._download(now["abi"], target["file"])
            self._step("stopping Kodi")
            await self.svc.agent.tv(lambda adb: adb.run(["am", "force-stop", PACKAGE]))
            if now["installed"] and now["version"]:
                self._step(f"backing up Kodi {now['version']}'s settings, libraries and add-ons on the TV")
                await asyncio.to_thread(backup, self.adb, now["version"])
            if downgrade or replace:
                self._step(f"uninstalling Kodi {now['version']}")
                await self.svc.agent.tv(lambda adb: adb.run(["pm", "uninstall", PACKAGE], timeout=60))
            self._step(f"installing Kodi {target['label']}")
            out = await asyncio.to_thread(install_apk, self.adb.host, self.svc.cfg.adb_port, apk)
            if "UPDATE_INCOMPATIBLE" in out or "INCONSISTENT_CERTIFICATES" in out:
                self.job["needs"] = "replace"
                raise RuntimeError("this Kodi is signed differently from the one installed (Google Play's): replacing "
                                   "it means uninstalling it first; its backup is restored afterwards")
            if "Success" not in out:
                raise RuntimeError(f"install failed: {out.strip()[-160:]}")
            if downgrade or replace:
                version = next((v for v in sorted(now["backups"], reverse=True) if v.split(".")[0] == to), None) \
                    if downgrade else now["version"]
                if version:
                    self._step(f"restoring the backup from Kodi {version}")
                    await asyncio.to_thread(restore, self.adb, version)
            # Kodi 22 RC1 crashes if its window goes while it starts (seen with the TV in standby): the TV is woken
            # first, and Kodi left for its first opening if it stays asleep
            await self.svc.agent.tv(lambda adb: adb.run(["input", "keyevent", "KEYCODE_WAKEUP"]))
            await asyncio.sleep(2)
            if await asyncio.to_thread(asleep, self.adb):
                self.job["note"] = "The TV is in standby: Kodi finishes updating the first time it's opened"
            else:
                self._step("starting Kodi (it updates its databases the first time)")
                try:
                    await self.svc.agent.kodi.start(wait_s=90)
                except Exception as e:
                    log.info("Kodi after the switch: %s", e)
                    self.job["note"] = "Kodi is installed but didn't answer yet: open it once on the TV"
            self.job["done"] = True
            self._step(f"Kodi {target['label']} is installed")
        except Exception as e:
            self.job["error"] = str(e) or type(e).__name__
            log.warning("Kodi switch to %s: %s", to, self.job["error"])
        finally:
            if apk:
                os.unlink(apk)

    async def _download(self, abi: str, name: str) -> str:
        url = MIRROR.format(dir=ABI_DIRS.get(abi, "arm")) + name
        http = self.svc.agent.http
        r = await http.get(url + "?sha256", timeout=20)
        r.raise_for_status()
        want = r.text.split()[0].lower() if r.text.strip() else ""
        if not re.fullmatch(r"[0-9a-f]{64}", want):
            raise RuntimeError("Kodi's mirror gave no SHA-256 for it, so it isn't installed")
        fd, path = tempfile.mkstemp(prefix="kodi-", suffix=".apk", dir=config_mod.DATA_DIR)
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as f:
                async with http.stream("GET", url, timeout=120) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(1 << 20):
                        f.write(chunk)
                        digest.update(chunk)
            if digest.hexdigest() != want:
                raise RuntimeError("the download's SHA-256 isn't the mirror's: not installed")
        except BaseException:
            os.unlink(path)
            raise
        return path


def cur_backup_missing(now: dict, to: str) -> bool:
    return not any(v.split(".")[0] == to for v in now["backups"])


def asleep(adb) -> bool:
    return "mWakefulness=Awake" not in adb.run(["sh", "-c", "dumpsys power | grep -m1 mWakefulness="], timeout=10)


def backup(adb, version: str):
    """Kodi's userdata and add-ons (not its cache and logs) in a tar on the TV, outside Kodi's own folder; checked
    to be there, just written, and not tiny."""
    path = f"{BACKUPS}/kodi-{version}.tar"
    out = adb.run(["sh", "-c", f"mkdir -p {BACKUPS} && tar -cf {path}.tmp -C {DATA} .kodi/userdata .kodi/addons "
                               f"&& mv -f {path}.tmp {path} && echo BACKUP $(date +%s) $(stat -c '%Y %s' {path})"],
                  timeout=120)
    m = re.search(r"BACKUP (\d+) (\d+) (\d+)", out)
    if not m or int(m.group(1)) - int(m.group(2)) > 60 or int(m.group(3)) < 100_000:
        raise RuntimeError(f"the backup failed: {out.strip()[-120:]}")


def restore(adb, version: str):
    out = adb.run(["sh", "-c", f"mkdir -p {DATA} && tar -xf {BACKUPS}/kodi-{version}.tar -C {DATA} && echo restored"],
                  timeout=120)
    if "restored" not in out:
        raise RuntimeError(f"restoring the backup failed: {out.strip()[-120:]}")


def install_apk(host: str, port: int, apk: str) -> str:
    """adb install over the TV's address (adb_shell's push is too slow for 70 MB); always this exact device."""
    adb = shutil.which("adb")
    if not adb:
        raise RuntimeError("adb (Android platform-tools) isn't installed on this computer")
    target = f"{host}:{port}"
    subprocess.run([adb, "connect", target], capture_output=True, text=True, timeout=20)
    r = subprocess.run([adb, "-s", target, "install", "-r", apk], capture_output=True, text=True, timeout=300)
    return r.stdout + r.stderr


def routes(svc) -> APIRouter:
    r = APIRouter()

    def panel_only(request: Request):
        from .clients import DEVICE
        svc.check(request, lan=False)
        if request.scope.get(DEVICE):
            raise HTTPException(403)

    @r.get("/v1/kodi")
    async def kodi_state(request: Request):
        panel_only(request)
        return await svc.kodi_versions.state()

    @r.post("/v1/kodi/update")
    async def kodi_update(request: Request):
        """To the newest Kodi on the mirrors (a backup on the TV first)."""
        panel_only(request)
        body = await request.json()
        state = await svc.kodi_versions.state()
        if not state.get("newer"):
            raise HTTPException(409, "Kodi is up to date" if state.get("latest") else
                                state.get("error") or "Kodi's mirrors didn't answer")
        return await start(state["latest"]["major"], bool(body.get("replace")))

    @r.post("/v1/kodi/switch")
    async def kodi_switch(request: Request):
        panel_only(request)
        body = await request.json()
        to = str(body.get("to", ""))
        if to not in NAMES:
            raise HTTPException(422, f"to: one of {', '.join(NAMES)}")
        return await start(to, bool(body.get("replace")))

    async def start(to: str, replace: bool) -> dict:
        job = svc.kodi_versions.job
        if job and not job["done"] and not job["error"]:
            raise HTTPException(409, "a switch is running already")
        if svc._busy.locked():
            raise HTTPException(409, "The TV app is listening: try again in a moment")
        asyncio.create_task(svc.kodi_versions.switch(to, replace=replace))
        return {"ok": True, "to": to}

    return r
