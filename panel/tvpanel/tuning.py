"""“This was right” and “This was wrong, it should be…” for a command from the history, from the Activity and the
Decisions tabs: it becomes a test of its own, with what it should do (what it did, or what you say it should).

- Its recording kept: a voice test (voicetests.py: the audio is kept with the tests; scripts/bench_voice.py).
- Typed (no recording): one of your sentences (voicetests.SENTENCES; scripts/bench_chains.py).
- An episode it found by what happens in it, or a moment it jumped to: a case for scripts/bench_finding.py
  (voicetests.FINDING), with the right episode or time (what it found, or what you say).
Your cases stay in ~/.local/share/tv-agent/voice-tests, never in the repo. “Wrong” marks the command, and teaches the
words what they should do (learned.json) unless you untick it."""
import html

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox,
                               QVBoxLayout)

from .common import GREEN, agent_activity, agent_voicetests, api


def this_was_right(parent, e: dict) -> str | None:
    """What it did is right: kept as a test (the words said, as you correct them)."""
    expect, why = agent_voicetests.expected({**e, "wrong": False}, agent_voicetests.learned(), asked=True)
    if expect is None:
        QMessageBox.information(parent, "TV Agent", f"Not added: {why}.")
        return None
    box = QDialog(parent)
    box.setWindowTitle("This was right")
    lay = QVBoxLayout(box)
    did = "As taught, it does" if why == "taught" else "It did"
    lay.addWidget(QLabel(f"{did}: <b>{html.escape(agent_voicetests.describe(expect))}</b>. That becomes a test."))
    form = QFormLayout()
    words = QLineEdit(e.get("heard", ""))
    form.addRow("What was said, word for word", words)
    lay.addLayout(form)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(box.accept)
    buttons.rejected.connect(box.reject)
    lay.addWidget(buttons)
    if box.exec() != QDialog.DialogCode.Accepted:
        return None
    added = agent_voicetests.keep_command(e, expect, words.text(), True)
    if e.get("wrong"):
        agent_activity.mark(float(e["t"]), False, heard=e.get("heard"))
    return f"<span style='color:{GREEN}'>Kept as {', '.join(added) or 'nothing (no recording, no words)'}</span>"


def this_was_wrong(parent, e: dict) -> str | None:
    """What it should have done (a command said the way the agent understands it), the words said, the right episode
    or moment when it searched for one: marked wrong, taught (unless unticked) and kept as a test."""
    box = QDialog(parent)
    box.setWindowTitle("This was wrong")
    lay = QVBoxLayout(box)
    form = QFormLayout()
    words = QLineEdit(e.get("heard", ""))
    form.addRow("What was said, word for word", words)
    should = QLineEdit()
    should.setPlaceholderText("say it the way the agent understands: “pause”, “play the bear season 3”")
    form.addRow("It should have done", should)
    preview = QLabel("")
    form.addRow("", preview)
    ep, sc = agent_voicetests.asked(e, "episode"), agent_voicetests.asked(e, "scene")
    episode = QLineEdit()
    moment = QLineEdit()
    if ep:
        episode.setPlaceholderText(f"e.g. S2E5 (it played {ep.get('found') or 'none'})")
        form.addRow("The right episode", episode)
    if sc:
        moment.setPlaceholderText(f"m:ss or h:mm:ss (it went to {sc.get('found') or 'none'})")
        form.addRow("The right moment", moment)
    teach = QCheckBox("Teach it too: from now on these words do that")
    teach.setChecked(True)
    form.addRow("", teach)
    lay.addLayout(form)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(box.accept)
    buttons.rejected.connect(box.reject)
    lay.addWidget(buttons)
    decided: dict = {}

    def check():
        text = should.text().strip()
        if not text:
            preview.setText("")
            return
        try:
            d = api("/v1/decide", {"text": text})
        except Exception as err:
            preview.setText(f"The voice agent isn't answering: {html.escape(str(err))}")
            return
        decided.clear()
        if d["action"]["type"] != "none":
            decided.update(d)
        preview.setText(f"<span style='color:gray'>→ {html.escape(d.get('describe', ''))} "
                        f"({d['action']['type'].replace('_', ' ')})</span>")
    should.editingFinished.connect(check)
    while True:
        if box.exec() != QDialog.DialogCode.Accepted:
            return None
        check()
        if decided or (not should.text().strip() and (episode.text().strip() or moment.text().strip())):
            break
        QMessageBox.information(parent, "TV Agent", "Say what it should have done the way the agent understands it, "
                                                    "or give the right episode or moment.")
    # only the episode or the moment was wrong: what it decided was right
    expect = agent_voicetests._plain(decided["action"] if decided else e.get("action") or {}) or None
    agent_activity.mark(float(e["t"]), True, heard=e.get("heard"))
    if decided and teach.isChecked() and e.get("heard", "").strip():
        try:
            api("/v1/learn", {"say": e["heard"].strip(), "do": decided["action"], "t": e["t"], "heard": e.get("heard")})
        except Exception as err:
            QMessageBox.warning(parent, "TV Agent", f"Not taught: {err}")
    added = agent_voicetests.keep_command(e, expect, words.text(), False, episode.text().strip() or None,
                                          moment.text().strip() or None)
    return (f"<span style='color:{GREEN}'>Marked wrong"
            + (f", taught: {html.escape(decided.get('describe', ''))}" if decided and teach.isChecked() else "")
            + (f"; kept as {', '.join(added)}" if added else "") + "</span>")
