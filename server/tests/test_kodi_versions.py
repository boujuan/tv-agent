"""Switching Kodi between 21 and 22 on the TV (kodi_versions.py): the right build, a backup first, going back."""
import asyncio
import hashlib
import types

import httpx

from tv_agent import config
from tv_agent import kodi_versions as K

LISTING = "".join(f'<a href="{n}">{n}</a>' for n in (
    "kodi-21.2-Omega-armeabi-v7a.apk", "kodi-21.3-Omega-armeabi-v7a.apk", "kodi-21.0-Omega_rc2-armeabi-v7a.apk",
    "kodi-22.0-Piers_beta2-armeabi-v7a.apk", "kodi-22.0-Piers_rc1-armeabi-v7a.apk", "kodi-22.0-Piers_rc1-arm64-v8a.apk"))
APK = b"PK\x03\x04 kodi 22"


def test_the_newest_build_of_each_version_for_the_tvs_abi():
    got = K.newest(LISTING, "armeabi-v7a")
    assert got["21"]["file"] == "kodi-21.3-Omega-armeabi-v7a.apk" and got["21"]["stable"]
    assert got["22"]["file"] == "kodi-22.0-Piers_rc1-armeabi-v7a.apk" and got["22"]["label"] == "22.0 RC 1 (Piers)"
    assert K.parse("kodi-22.0-Piers-armeabi-v7a.apk")["key"] > K.parse("kodi-22.0-Piers_rc1-armeabi-v7a.apk")["key"]


DUMPSYS = """    versionCode=2102000 minSdk=21 targetSdk=34
    versionName=21.2
    primaryCpuAbi=armeabi-v7a
    installerPackageName=com.android.vending
ABIS armeabi-v7a,armeabi
BACKUP /sdcard/tv-agent/kodi-21.2.tar 17894400 1792758087
"""


class FakeAdb:
    def __init__(self, version="21.2", backups=("21.2",)):
        self.version, self.backups, self.did, self.host = version, set(backups), [], "192.168.1.40"

    def run(self, argv, timeout=10.0):
        line = " ".join(argv)
        self.did.append(line)
        if "dumpsys package" in line:
            return (f"    versionCode={self.version.replace('.', '0')}000\n    versionName={self.version}\n"
                    "    primaryCpuAbi=armeabi-v7a\n    installerPackageName=com.android.vending\nABIS armeabi-v7a\n"
                    + "".join(f"BACKUP /sdcard/tv-agent/kodi-{b}.tar 100 1\n" for b in sorted(self.backups)))
        if "tar -cf" in line:
            self.backups.add(self.version)
            return "BACKUP 1792758087 1792758086 17894400"
        if "dumpsys power" in line:
            return "  mWakefulness=Asleep" if getattr(self, "sleeping", False) else "  mWakefulness=Awake"
        if "tar -xf" in line:
            return "restored"
        return ""


def test_the_tv_says_what_kodi_it_has():
    adb = types.SimpleNamespace(run=lambda argv, timeout=10: DUMPSYS)
    got = K.installed(adb)
    assert got["version"] == "21.2" and got["abi"] == "armeabi-v7a" and got["from"] == "com.android.vending"
    assert got["backups"] == {"21.2": {"size": 17894400, "made": 1792758087}}


def switch(tmp_path, monkeypatch, to, adb, install_out="Success", replace=False, apk=APK):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    async def no_wait(s):
        return None
    monkeypatch.setattr(K.asyncio, "sleep", no_wait)

    def handler(request: httpx.Request):
        if request.url.path.endswith("/"):
            return httpx.Response(200, text=LISTING)
        if request.url.query == b"sha256":
            return httpx.Response(200, text=hashlib.sha256(APK).hexdigest() + "  x.apk")
        return httpx.Response(200, content=apk)
    installs = []

    def install(host, port, path):
        installs.append(open(path, "rb").read())
        if "Success" in install_out:
            adb.version = "22.0" if "22" in path or to == "22" else "21.3"
        return install_out

    async def tv(fn, *args):
        return fn(adb, *args)

    async def start(wait_s=20):
        adb.did.append("kodi start")
    monkeypatch.setattr(K, "install_apk", install)
    agent = types.SimpleNamespace(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), adb=adb, tv=tv,
                                  kodi=types.SimpleNamespace(start=start))
    kv = K.KodiVersions(types.SimpleNamespace(agent=agent, cfg=types.SimpleNamespace(adb_port=5555)))
    asyncio.run(kv.switch(to, replace=replace))
    return kv.job, installs


def test_an_upgrade_backs_up_first_and_installs_the_checked_apk(tmp_path, monkeypatch):
    adb = FakeAdb(backups=())
    job, installs = switch(tmp_path, monkeypatch, "22", adb)
    assert job["done"] and not job["error"] and installs == [APK]
    stop, back = adb.did.index("am force-stop org.xbmc.kodi"), next(i for i, d in enumerate(adb.did) if "tar -cf" in d)
    assert stop < back and "21.2" in adb.backups and not any("uninstall" in d for d in adb.did)
    assert not list(tmp_path.glob("*.apk"))                   # the download doesn't stay


def test_a_download_with_another_checksum_is_never_installed(tmp_path, monkeypatch):
    job, installs = switch(tmp_path, monkeypatch, "22", FakeAdb(), apk=b"something else")
    assert job["error"] and "SHA-256" in job["error"] and installs == []


def test_kodi_from_google_play_signed_otherwise_asks_to_be_replaced(tmp_path, monkeypatch):
    adb = FakeAdb()
    job, _ = switch(tmp_path, monkeypatch, "22", adb, install_out="Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]")
    assert job["needs"] == "replace" and not any("uninstall" in d for d in adb.did)
    adb = FakeAdb()
    job, _ = switch(tmp_path, monkeypatch, "22", adb, replace=True)     # confirmed: uninstall, install, restore
    did = " | ".join(adb.did)
    assert job["done"] and did.index("pm uninstall") < did.index("tar -xf /sdcard/tv-agent/kodi-21.2.tar")


def test_going_back_restores_the_backup_from_21_and_refuses_without_one(tmp_path, monkeypatch):
    adb = FakeAdb(version="22.0", backups=("21.2",))
    job, _ = switch(tmp_path, monkeypatch, "21", adb)
    did = " | ".join(adb.did)
    assert job["done"] and did.index("kodi-22.0.tar") < did.index("pm uninstall") < did.index("tar -xf /sdcard/tv-agent/kodi-21.2.tar")
    job, installs = switch(tmp_path, monkeypatch, "21", FakeAdb(version="22.0", backups=()))
    assert job["error"] and "no backup" in job["error"] and installs == []


def test_the_newest_kodi_is_offered_over_an_older_one(monkeypatch):
    def handler(request: httpx.Request):
        return httpx.Response(200, text=LISTING)
    adb = FakeAdb()

    async def tv(fn, *args):
        return fn(adb, *args)
    agent = types.SimpleNamespace(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), adb=adb, tv=tv)
    kv = K.KodiVersions(types.SimpleNamespace(agent=agent, cfg=types.SimpleNamespace(adb_port=5555)))
    st = asyncio.run(kv.state())
    assert st["newer"] and st["latest"]["major"] == "22" and st["latest"]["label"] == "22.0 RC 1 (Piers)"
    adb.version = "22.0"
    kv._available = None
    assert not asyncio.run(kv.state())["newer"]


def test_the_episode_check_can_be_turned_off_live():
    from tv_agent import remote
    assert remote.setting("check_episodes", False) is False


def test_kodis_own_version_names_compare_with_the_mirrors():
    assert K.version_key("22.0-RC1") == K.parse("kodi-22.0-Piers_rc1-armeabi-v7a.apk")["key"]
    assert K.parse("kodi-22.0-Piers-armeabi-v7a.apk")["key"] > K.version_key("22.0-RC1") > K.version_key("21.2")


def test_a_tv_in_standby_isnt_given_kodi_to_start(tmp_path, monkeypatch):
    adb = FakeAdb(backups=())
    adb.sleeping = True
    job, installs = switch(tmp_path, monkeypatch, "22", adb)
    assert job["done"] and installs == [APK] and "standby" in job["note"] and "kodi start" not in adb.did
    assert any("KEYCODE_WAKEUP" in d for d in adb.did)


def test_a_backup_that_wasnt_written_stops_the_update(tmp_path, monkeypatch):
    adb = FakeAdb(backups=())
    real = adb.run
    adb.run = lambda argv, timeout=10.0: "tar: no space left" if "tar -cf" in " ".join(argv) else real(argv, timeout)
    job, installs = switch(tmp_path, monkeypatch, "22", adb)
    assert job["error"] and "backup failed" in job["error"] and installs == []
