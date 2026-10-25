"""The panel offscreen: every tab is built and draws; Settings has a control for every setting the agent has (or it's
in Decisions) and saves live what the running agent takes live, restarting it only for the rest; the laptop
microphone ends a command like the TV app; About shows TV Agent Player and the phones; the Commands tab marks what
only the LLM decides; Settings › Backup shows the last backup and a restore's dry run."""
import ast
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from tvpanel import about, backup_card, common, mic, settings_tab
from tvpanel.app import Panel

SERVER = Path(__file__).resolve().parents[2] / "server/src/tv_agent"
# Changed live in Decisions (/v1/pipeline), not in Settings
IN_DECISIONS = {"stt_model", "stt_where", "stt_hosted_model", "stt_language", "stt_vocabulary", "stt_vocabularies",
                "stt_vocabulary_library", "keep_recordings", "decide_steps", "decide_off", "d1_where", "d1_model",
                "hosted_d1_model", "llm_model", "llm_reasoning", "llm_private"}


def constant(path: Path, name: str):
    """A tuple of strings defined at the top of one of the agent's modules (they need FastAPI: not imported)."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


@pytest.fixture(scope="module")
def panel():
    app = QApplication.instance() or QApplication([])
    w = Panel(common.Prefs())
    yield w
    w.bg.pool.shutdown(wait=False, cancel_futures=True)
    w.deleteLater()
    app.processEvents()


def test_every_tab_is_built_and_draws(panel):
    names = [panel.tabs.tabText(i) for i in range(panel.tabs.count())]
    assert names == ["Overview", "Activity", "Quality", "Commands", "Decisions", "Settings"]
    for i in range(panel.tabs.count()):
        panel.tabs.setCurrentIndex(i)
        page = panel.tabs.widget(i)
        page.resize(920, 800)
        assert not page.grab().isNull()
    assert [panel.decisions.tabs.tabText(i) for i in range(panel.decisions.tabs.count())] == [
        "Speech to text", "Clean-up", "Fixed phrases", "Pattern rules", "LLM", "Decision model", "Apps && hooks"]


def test_settings_has_a_control_for_every_setting(panel):
    fields = set(common.agent_config.Config.__dataclass_fields__) - {"secrets"}
    assert fields - IN_DECISIONS - set(panel.settings.values()) == set()
    assert {k for k, *_ in settings_tab.ADVANCED} <= fields


def test_what_saves_live_is_what_the_agent_takes_live():
    assert settings_tab.LIVE == constant(SERVER / "remote.py", "SETTINGS")
    assert settings_tab.REREAD == constant(SERVER / "server.py", "ADDRESS_SETTINGS")
    live, restart = settings_tab.how_to_apply({"binge": False, "tv_hosts": ["192.0.2.2"], "lan_port": 8000,
                                               "phones": False})
    assert live == {"binge": False} and restart == ["lan_port", "phones"]


def test_save_writes_only_what_changed_and_restarts_only_when_needed(panel, monkeypatch):
    s = panel.settings
    agent = panel.overview.cards["tv-agent"]
    agent.info = {"ActiveState": "active"}                # as if the voice agent runs (nothing is started)
    calls, restarts = [], []
    monkeypatch.setattr(settings_tab, "api", lambda path, body=None, **kw: calls.append((path, body)) or {})
    monkeypatch.setattr(agent, "act", lambda verb: restarts.append(verb))
    monkeypatch.setattr(panel.bg, "run", lambda fn, cb=None, *a: cb and cb(fn(*a), None))
    s.load()
    common.agent_config.set_value(common.load_cfg(), "auto_skip", True)    # said by voice since the tab loaded
    s.binge.setChecked(not s.binge.isChecked())
    assert s.save()
    cfg = common.load_cfg()
    assert cfg.binge == s.binge.isChecked() and cfg.auto_skip is True      # the voice's change wasn't undone
    assert calls == [("/v1/settings", {"binge": cfg.binge})] and restarts == []
    calls.clear()
    s.adv["lan_port"].setValue(8001)
    assert s.save()
    assert common.load_cfg().lan_port == 8001 and restarts == ["restart"] and calls == []
    restarts.clear()
    s.twitch.setText("someone")                           # an edit left unsaved while the tray changes the player
    s.mark_dirty()
    assert s.save({"player": "kodi"})
    assert common.load_cfg().player == "kodi" and s.twitch.text() == "someone" and s.dirty
    assert calls == [("/v1/settings", {"player": "kodi"})] and restarts == []
    s.load()
    agent.info = {}


def frames(endpointer: mic.Endpointer, levels: list[float]) -> tuple[str, int]:
    """What the endpointer said last, and after how many frames."""
    for n, db in enumerate(levels, 1):
        said = endpointer.feed(db)
        if said in ("end", "give up"):
            return said, n
    return said, len(levels)


def test_the_laptop_mic_ends_a_command_over_the_sound_around_it():
    # a near-silent start, a second of voice, then the TV's sound well above the threshold set at the start: quiet
    # against that sound (the TV app's rule since 0.9.2); before, it listened until the 10 s limit
    room, voice, tv = [-70.0] * 10, [-20.0] * 50, [-48.0] * 600
    said, n = frames(mic.Endpointer(), room + voice + tv)
    assert said == "end" and 60 + mic.END <= n <= 60 + mic.END + mic.START     # its last second fills first
    said, n = frames(mic.Endpointer(), room + voice + [-30.0] * 600)      # within 25 dB of the voice: still voice
    assert said == "wait"
    said, n = frames(mic.Endpointer(), [-70.0] * 300)
    assert said == "give up" and n == mic.GIVE_UP
    assert mic.MAX == 750                                 # 15 s, as the TV app (the agent takes 16)


def test_about_shows_the_player_and_the_phones():
    info = {"versions": {"panel": "0.10.0", "server": "0.12.0", "android": "0.9.2"}, "source": {}, "describe": None,
            "branch": None, "pinned": [], "github": {"error": "offline"},
            "agent": {"version": "0.12.0", "python": "3.13", "stt_model": "parakeet-v3", "stt_where": "hosted-first",
                      "stt_hosted_model": "x", "d1_model": "/m/d1.gguf", "d1_where": "local-first",
                      "hosted_d1_model": "d1", "llm_model": "deepseek", "player": "tvplayer"},
            "tv": {"tvplayer": {"installed": True, "version": "0.6.2"}, "tvagent": {"installed": False}},
            "updates": {"components": {"tvplayer": {"latest": "0.6.2"}, "phone": {"latest": "0.2.3"}}},
            "clients": {"devices": [{"name": "Pixel", "app_version": "0.2.3"}]}}
    text = about.render(info)
    assert "TV Agent Player" in text and "0.6.2 on the TV" in text and "0.2.3 on Pixel" in text
    assert "Hosted, this laptop if it fails" in text and "play in TV Agent Player" in text


def test_the_commands_tab_marks_what_only_the_llm_decides(panel, monkeypatch):
    from tvpanel import commands_tab
    page = panel.commands
    page.filter.setText("plot")
    html = page.view.toHtml()
    assert "the episode of" in html and ">LLM<" not in html      # the rules decide it since server 0.14.0
    section = commands_tab.SECTIONS[0]
    llm_only = section._replace(commands=[section.commands[0]._replace(by="llm")])
    monkeypatch.setattr(commands_tab, "SECTIONS", [llm_only])
    page.filter.setText("")
    assert ">LLM<" in page.view.toHtml()


def test_settings_backup_shows_the_last_one_and_a_restores_dry_run(panel):
    card = panel.settings.backup
    card.show_state(backup_card.state())
    assert "No backup yet" in card.last.text() and not card.restore_button.isEnabled()
    learned = common.agent_config.CONFIG_DIR / "learned.json"
    learned.write_text("[]")
    made = common.agent_backup.backup()            # into the test home's ~/tv-agent-backups
    card.show_state(backup_card.state())
    assert "Last backup" in card.last.text() and card.restore_button.isEnabled()
    assert str(common.agent_backup.DEFAULT_DIR) in card.folder.text() and "Not automatic" in card.auto.text()
    assert not card.warning.isVisible()
    learned.write_text('[{"text": "lights"}]')
    dialog = backup_card.RestoreDialog(card, card.found)
    dialog.show_plan(common.agent_backup.plan(made["name"], dialog.chosen()))
    assert dialog.list.count() == 1 and "config/learned.json" in dialog.list.item(0).text()
    assert "taught commands" in dialog.list.item(0).text() and "1 files" in dialog.summary.text()
    assert dialog.go.isEnabled()
    dialog.reject()
