"""Backups of this computer's setup (backup.py): what's in one and what's left out, private from the first byte,
skipped when nothing changed, hard links for what didn't, the rotation, the dry run, a restore into a temporary home
checked against the checksums, a damaged backup refused, and the voice agent stopped only while nothing plays."""
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tv_agent import backup as B
from tv_agent import cli, config, install


def make_home(home: Path) -> Path:
    cfg = home / ".config/tv-agent"
    cfg.mkdir(parents=True, mode=0o700)
    (cfg / "config.toml").write_text(f'tv_hosts = ["192.0.2.1"]\nlocal_port = 9\nadb_key = "{home}/.android/adbkey"\n')
    for name, text in (("secrets.toml", 'agent_token = "t0ken"\n'), ("clients.json", '{"devices": []}'),
                       ("phone-tls.key", "KEY")):
        (cfg / name).write_text(text)
        (cfg / name).chmod(0o600)
    (cfg / "learned.json").write_text('[{"text": "lights"}]')
    (cfg / "rules.saved-2026-10-22-2034.json").write_text("{}")
    (cfg / "now-playing.json").write_text('{"url": "https://debrid.example/secret-link"}')
    (cfg / "secrets.tmp").write_text("half")
    data = home / ".local/share/tv-agent"
    for d in ("voice-tests", "models/d1-3B", "shaders", "apk-justplayer", "signing", "quality/runs", "traces"):
        (data / d).mkdir(parents=True)
    (data / "voice-tests/a.wav").write_bytes(os.urandom(5000))
    (data / "voice-tests/b.wav").write_bytes(os.urandom(7000))
    (data / "voice-tests/labels.jsonl").write_text('{"file": "a.wav"}\n')
    (data / "models/d1-3B/d1.gguf").write_bytes(b"x" * 3000)
    (data / "shaders/blur.spv").write_bytes(b"s" * 10)
    (data / "apk-justplayer/just.apk").write_bytes(b"a" * 20)
    (data / "signing/android-release.p12").write_bytes(b"p12")
    (data / "signing/android-release.p12").chmod(0o600)
    (data / "quality/runs/2026-10-25.json").write_text("{}")
    (data / "activity.jsonl").write_text('{"t": 1}\n')
    (data / "tv-agent-x1y2.apk").write_bytes(b"update in progress")
    (home / ".android").mkdir()
    (home / ".android/adbkey").write_text("PRIVATE")
    (home / ".android/adbkey").chmod(0o600)
    (home / ".android/adbkey.pub").write_text("PUBLIC")
    (home / ".gradle").mkdir()
    (home / ".gradle/gradle.properties").write_text("org.gradle.jvmargs=-Xmx4g\nTV_AGENT_KEYSTORE=/k.p12\n"
                                                    "TV_AGENT_KEYSTORE_PASSWORD=pw\n")
    return home


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """This computer's setup, in a temporary home: what roots() means by "here"."""
    h = make_home(tmp_path / "home")
    monkeypatch.setattr(config, "CONFIG_DIR", h / ".config/tv-agent")
    monkeypatch.setattr(config, "DATA_DIR", h / ".local/share/tv-agent")
    real = B.roots
    monkeypatch.setattr(B, "roots", lambda home=None: real(home or h))
    monkeypatch.setattr(B, "_systemctl", lambda *a: pytest.fail(f"systemctl {a}"))
    return h


def private(path: Path) -> bool:
    return all(stat.S_IMODE(p.stat().st_mode) == (0o700 if p.is_dir() else 0o600)
               for p in [path, *path.rglob("*")])


def test_a_backup_has_the_setup_but_what_is_downloaded_again_and_is_private(home, tmp_path):
    res = B.backup(folder=tmp_path / "backups")
    assert not res["skipped"] and res["reason"] == "manual"
    snap = Path(res["path"])
    man = json.loads((snap / "manifest.json").read_text())
    files = {f["path"] for f in man["files"]}
    assert {"config/config.toml", "config/secrets.toml", "config/clients.json", "config/learned.json",
            "config/phone-tls.key", "config/rules.saved-2026-10-22-2034.json", "data/voice-tests/a.wav",
            "data/voice-tests/b.wav", "data/voice-tests/labels.jsonl", "data/signing/android-release.p12", "data/quality/runs/2026-10-25.json",
            "data/activity.jsonl", "adb/adbkey", "adb/adbkey.pub", B.GRADLE} == files
    assert {x["path"] for x in man["left_out"]} == {"config/now-playing.json", "data/models", "data/shaders",
                                                     "data/apk-justplayer"}
    assert next(x for x in man["left_out"] if x["path"] == "data/models")["size"] == 3000
    assert (snap / B.GRADLE).read_text() == "TV_AGENT_KEYSTORE=/k.p12\nTV_AGENT_KEYSTORE_PASSWORD=pw\n"
    assert "debrid" not in "".join(p.read_text(errors="ignore") for p in snap.rglob("*") if p.is_file())
    assert private(snap) and private(tmp_path / "backups")
    assert stat.S_IMODE((tmp_path / "backups").stat().st_mode) == 0o700
    assert B.verify(snap) == []
    assert man["versions"]["agent"] and "phones" in man["versions"]
    entry = next(f for f in man["files"] if f["path"] == "config/learned.json")
    learned = home / ".config/tv-agent/learned.json"
    assert entry["mode"] == f"{stat.S_IMODE(learned.stat().st_mode):o}" and entry["size"] == learned.stat().st_size


def test_nothing_changed_skips_and_what_did_not_change_is_linked(home, tmp_path):
    folder = tmp_path / "backups"
    first = B.backup(folder=folder, now=time.time() - 86400)          # yesterday's: one a day is kept
    assert B.backup(folder=folder)["skipped"]
    (home / ".config/tv-agent/learned.json").write_text('[{"text": "lights"}, {"text": "fan"}]')
    second = B.backup(folder=folder)
    a, b = Path(first["path"]), Path(second["path"])
    wav = "data/voice-tests/a.wav"
    assert (a / wav).stat().st_ino == (b / wav).stat().st_ino            # one copy on disk
    assert (a / "config/learned.json").stat().st_ino != (b / "config/learned.json").stat().st_ino
    assert second["new"] == len('[{"text": "lights"}, {"text": "fan"}]')
    assert B.usage(folder) < first["size"] + second["size"]
    assert B.verify(a) == [] and B.verify(b) == []
    assert not B.backup(folder=folder, force=True, now=time.time() + 1)["skipped"]       # one anyway


def test_rotation_keeps_the_last_days_weeks_and_safety_backups():
    day = 86400
    t0 = time.mktime(time.strptime("2026-10-25 12:00", "%Y-%m-%d %H:%M"))
    found = [{"name": f"b{i}", "t": t0 - i * day / 2, "reason": "daily"} for i in range(80)]      # two a day
    found += [{"name": f"s{i}", "t": t0 - i * day, "reason": B.SAFETY} for i in range(5)]
    found.sort(key=lambda b: b["t"], reverse=True)
    kept = B.keep(found, 7, 4)
    days = {time.strftime("%Y-%m-%d", time.localtime(b["t"])) for b in found if b["name"] in kept and
            b["reason"] == "daily"}
    assert {f"b{i}" for i in range(0, 14, 2)} <= kept                    # the newest of each of the last 7 days
    assert not {"b1", "b3"} & kept                                       # not the day's older ones
    assert {"s0", "s1", "s2"} <= kept and not {"s3", "s4"} & kept
    assert len(days) == 10                                               # and the 3 weeks before's newest
    weeks = {time.strftime("%G-%V", time.localtime(b["t"])) for b in found if b["name"] in kept}
    assert len(weeks) == 4


def test_old_backups_are_removed_when_a_new_one_is_made(home, tmp_path):
    folder = tmp_path / "backups"
    sunday = time.mktime(time.strptime("2026-10-25 12:00", "%Y-%m-%d %H:%M"))
    removed = []
    for i in range(10, -1, -1):         # one a day, from Thursday 15 October
        (home / ".local/share/tv-agent/activity.jsonl").write_text(f'{{"t": {i}}}\n')
        removed += B.backup(folder=folder, now=sunday - i * 86400)["removed"]
    days = [b["name"][:10] for b in B.backups(folder)]
    assert days == [f"2026-10-{d}" for d in range(25, 17, -1)]       # the last 7 days, and the week before's newest
    assert [r[:10] for r in removed] == ["2026-10-15", "2026-10-16", "2026-10-17"]
    assert all(B.verify(b["path"]) == [] for b in B.backups(folder))    # the kept ones whole, links and all


def test_restore_into_a_temporary_home_matches_the_checksums(home, tmp_path):
    folder = tmp_path / "backups"
    res = B.backup(folder=folder)
    other = tmp_path / "other"
    (other / ".gradle").mkdir(parents=True)
    (other / ".gradle/gradle.properties").write_text("org.gradle.daemon=true\nTV_AGENT_KEYSTORE=/old.p12")
    plan = B.plan(res["name"], home=other, folder=folder)
    assert len(plan["changes"]) == res["files"] and plan["same"] == 0
    assert {c["change"] for c in plan["changes"]} == {"new", "changed"}       # gradle.properties had a line
    done = B.restore_files(res["name"], home=other, folder=folder)
    assert len(done["restored"]) == res["files"]
    man = json.loads((Path(res["path"]) / "manifest.json").read_text())
    at = B.roots(other)
    for f in man["files"]:
        assert B._digest(f["path"], B.where(f["path"], at)) == f["sha256"], f["path"]
    assert B.plan(res["name"], home=other, folder=folder)["changes"] == []
    assert (other / ".config/tv-agent/secrets.toml").read_text() == 'agent_token = "t0ken"\n'
    assert stat.S_IMODE((other / ".config/tv-agent").stat().st_mode) == 0o700
    for secret in (".config/tv-agent/secrets.toml", ".config/tv-agent/phone-tls.key", ".android/adbkey",
                   ".local/share/tv-agent/signing/android-release.p12", ".gradle/gradle.properties"):
        assert stat.S_IMODE((other / secret).stat().st_mode) == 0o600, secret
    assert stat.S_IMODE((other / ".config/tv-agent/learned.json").stat().st_mode) == \
        stat.S_IMODE((home / ".config/tv-agent/learned.json").stat().st_mode)                   # as it was
    assert (other / ".gradle/gradle.properties").read_text() == (
        "org.gradle.daemon=true\nTV_AGENT_KEYSTORE=/k.p12\nTV_AGENT_KEYSTORE_PASSWORD=pw\n")
    assert not (other / ".config/tv-agent/now-playing.json").exists()
    assert not (other / ".local/share/tv-agent/models").exists()


def test_the_dry_run_lists_what_would_change_and_parts_can_be_chosen(home, tmp_path):
    folder = tmp_path / "backups"
    name = B.backup(folder=folder)["name"]
    (home / ".config/tv-agent/learned.json").write_text("[]")
    (home / ".local/share/tv-agent/voice-tests/b.wav").unlink()
    (home / ".local/share/tv-agent/voice-tests/c.wav").write_bytes(b"new since")
    plan = B.plan(name, folder=folder)
    changes = {c["path"]: c for c in plan["changes"]}
    assert changes.keys() == {"config/learned.json", "data/voice-tests/b.wav"}
    assert changes["config/learned.json"]["change"] == "changed" and changes["config/learned.json"]["what"] == \
        "taught commands"
    assert changes["data/voice-tests/b.wav"]["change"] == "new" and changes["data/voice-tests/b.wav"]["what"] == \
        "voice tests"
    assert plan["kept"] == ["data/voice-tests/c.wav"]
    only = B.plan(name, ["data/voice-tests"], folder=folder)
    assert [c["path"] for c in only["changes"]] == ["data/voice-tests/b.wav"]
    assert B.plan("latest", ["adb", "gradle"], folder=folder)["changes"] == []
    assert (home / ".config/tv-agent/learned.json").read_text() == "[]"      # a dry run writes nothing


def test_tv_agent_players_settings_go_back_to_it_after_a_restore(home, tmp_path):
    data = home / ".local/share/tv-agent"
    (data / "tvplayer-settings.json").write_text('{"values": {"sub_size": 150}}')
    (data / "tvplayer-settings.restored.json").write_text('{"values": {"sub_size": 50}}')    # an older restore's
    folder = tmp_path / "backups"
    res = B.backup(folder=folder)
    man = json.loads((Path(res["path"]) / "manifest.json").read_text())
    assert B.PLAYER_SETTINGS in {f["path"] for f in man["files"]}
    assert "data/tvplayer-settings.restored.json" in {x["path"] for x in man["left_out"]}
    other = tmp_path / "other"
    done = B.restore_files(res["name"], home=other, folder=folder)
    assert B.PLAYER_SETTINGS in done["restored"]
    assert (other / ".local/share/tv-agent/tvplayer-settings.restored.json").read_text() == \
        '{"values": {"sub_size": 150}}'                      # for the player's next connection (tv/tvplayer.py)


def test_a_damaged_backup_is_not_restored(home, tmp_path):
    folder = tmp_path / "backups"
    res = B.backup(folder=folder)
    snap = Path(res["path"])
    (snap / "config/learned.json").chmod(0o600)
    (snap / "config/learned.json").write_text("[] tampered")
    (snap / "data/voice-tests/a.wav").unlink()
    assert sorted(B.verify(snap)) == ["config/learned.json: changed since the backup", "data/voice-tests/a.wav: missing"]
    other = tmp_path / "other"
    with pytest.raises(B.BackupError, match="isn't as it was made"):
        B.restore_files(res["name"], home=other, folder=folder)
    assert not other.exists()


def test_a_backup_cannot_send_a_restore_outside_its_parts(home, tmp_path):
    folder = tmp_path / "backups"
    snap = Path(B.backup(folder=folder)["path"])
    man = json.loads((snap / "manifest.json").read_text())
    man["files"].append({"path": "config/../../.bashrc", "size": 1, "sha256": "0", "mode": "644"})
    (snap / "manifest.json").write_text(json.dumps(man))
    with pytest.raises(B.BackupError, match="outside what a backup holds"):
        B.plan(snap.name, folder=folder)
    assert "config/../../.bashrc: not a path a backup holds" in B.verify(snap)
    assert [B.inside(x) for x in ("config/a.json", "data/voice-tests/a.wav", B.GRADLE, "gradle/init.gradle",
                                   "adb/adbkey", "home/.bashrc", "data/", "data/./x")] == \
        [True, True, True, False, True, False, False, False]


def test_a_restore_stops_the_agent_only_while_nothing_plays(home, tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    name = B.backup(folder=folder, now=time.time() - 5)["name"]
    learned = home / ".config/tv-agent/learned.json"
    learned.write_text("[]")
    calls = []
    monkeypatch.setattr(B, "_systemctl", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(B, "agent_active", lambda: True)
    monkeypatch.setattr(B, "playing", lambda: "Dune is paused on the TV")
    with pytest.raises(B.BackupError, match="Dune is paused on the TV: restore once it's stopped"):
        B.restore(name, folder=folder)
    assert calls == [] and learned.read_text() == "[]" and len(B.backups(folder)) == 1
    monkeypatch.setattr(B, "playing", lambda: None)
    res = B.restore(name, folder=folder)
    assert calls == [("stop", "tv-agent.service"), ("start", "tv-agent.service")]
    assert res["restored"] == ["config/learned.json"] and res["agent"] == "restarted"
    assert learned.read_text() == '[{"text": "lights"}]'
    safety = B.find(res["safety"], folder)
    assert res["safety"].startswith("safety-")
    assert json.loads((safety / "config/learned.json").read_text()) == []      # what was here, kept
    assert B.find("latest", folder).name == name                                 # latest: not a safety one
    calls.clear()
    assert B.restore(name, folder=folder)["agent"] == "untouched" and calls == []      # nothing to do


def test_a_restore_with_the_agent_failing_to_stop_writes_nothing_and_starts_it(home, tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    name = B.backup(folder=folder, now=time.time() - 5)["name"]
    (home / ".config/tv-agent/learned.json").write_text("[]")
    calls = []
    monkeypatch.setattr(B, "_systemctl", lambda *a: calls.append(a) or subprocess.CompletedProcess(
        a, 1 if a[0] == "stop" else 0, "", "Access denied"))
    monkeypatch.setattr(B, "agent_active", lambda: True)
    monkeypatch.setattr(B, "playing", lambda: None)
    with pytest.raises(B.BackupError, match="didn't stop: Access denied"):
        B.restore(name, folder=folder)
    assert calls[-1] == ("start", "tv-agent.service")
    assert (home / ".config/tv-agent/learned.json").read_text() == "[]"


def test_playing_asks_the_running_agent(monkeypatch):
    monkeypatch.setattr(B, "_get", lambda path, timeout=3: None)
    with pytest.raises(B.BackupError, match="isn't answering"):
        B.playing()
    monkeypatch.setattr(B, "_get", lambda path, timeout=3: {"now_playing": None, "player": None})
    assert B.playing() is None
    monkeypatch.setattr(B, "_get", lambda path, timeout=3: {"now_playing": {"name": "Dune"},
                                                             "player": {"app": "tvplayer", "playing": True}})
    assert B.playing() == "Dune is playing on the TV"


def test_what_else_covers_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    folder = tmp_path / "Coding/Export/backups"
    folder.mkdir(parents=True)
    assert B.covered(folder) == []
    (tmp_path / "Coding/.stfolder").mkdir()
    assert B.covered(folder) == [f"Syncthing ({tmp_path / 'Coding'})"]
    (tmp_path / "Coding/.stfolder").rmdir()
    repo = tmp_path / "Coding"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert B.covered(folder) == [f"a git repository ({repo})"]
    (repo / ".gitignore").write_text("Export/\n")
    assert B.covered(folder) == []                    # ignored: never committed
    box = tmp_path / "Dropbox/tv"
    box.mkdir(parents=True)
    assert B.covered(box) == [f"Dropbox's synced folder ({tmp_path / 'Dropbox'})"]


def test_settings_say_where_backups_go(home, tmp_path):
    assert B.settings()["dir"] == B.DEFAULT_DIR and B.settings()["keep_daily"] == 7
    B.save_settings(dir=tmp_path / "elsewhere")
    assert B.settings() == {"dir": tmp_path / "elsewhere", "keep_daily": 7, "keep_weekly": 4}
    (config.CONFIG_DIR / "backup.json").write_text('{"dir": "~/b", "keep_daily": 0, "keep_weekly": 2}')
    assert B.settings() == {"dir": Path.home() / "b", "keep_daily": 7, "keep_weekly": 2}


def test_the_command_line_backs_up_lists_and_shows_a_dry_run(home, tmp_path, capsys):
    folder = str(tmp_path / "backups")
    assert cli.main(["backup", "--to", folder]) == 0
    out = capsys.readouterr().out
    assert "Backed up to" in out and "15 files" in out and "Left out: models 3 KB, now-playing.json" in out
    assert cli.main(["backup", "--to", folder]) == 0
    assert "Nothing changed since the last backup" in capsys.readouterr().out
    assert cli.main(["backup", "list", "--to", folder, "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1 and listed[0]["files"] == 15
    assert cli.main(["backup", "verify", "--to", folder]) == 0
    assert "files as they were written" in capsys.readouterr().out
    (home / ".config/tv-agent/learned.json").write_text("[]")
    assert cli.main(["backup", "restore", "latest", "--to", folder, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Would write 1 files" in out and "config/learned.json" in out and "taught commands" in out
    assert cli.main(["backup", "restore", "latest", "--to", folder, "--dry-run", "--json"]) == 0
    assert [c["path"] for c in json.loads(capsys.readouterr().out)["changes"]] == ["config/learned.json"]
    assert cli.main(["backup", "restore", "nope", "--to", folder, "--json"]) == 1
    assert "no backup 'nope'" in json.loads(capsys.readouterr().out)["error"]


def test_the_timer_runs_it_daily(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "UNIT_DIR", tmp_path)
    monkeypatch.setattr(install, "tv_agent_command", lambda: Path("/opt/tv-agent/bin/tv-agent"))
    ran = []
    monkeypatch.setattr(install.subprocess, "run", lambda args, check: ran.append(args))
    install.write_backups(enable=True)
    assert "ExecStart=/opt/tv-agent/bin/tv-agent backup --daily" in (tmp_path / "tv-agent-backup.service").read_text()
    timer = (tmp_path / "tv-agent-backup.timer").read_text()
    assert "OnCalendar=*-*-* 05:15:00" in timer and "Persistent=true" in timer
    assert ran == [["systemctl", "--user", "daemon-reload"],
                   ["systemctl", "--user", "enable", "--now", "tv-agent-backup.timer"]]
