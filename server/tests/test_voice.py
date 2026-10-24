"""Voice tests (voicetests.py, scripts/bench_voice.py) without audio: labels from the history, comparing runs."""
import json
import sys
from pathlib import Path

import pytest

from tv_agent import actions as A
from tv_agent import voicetests as V

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bench_voice  # noqa: E402

BEAR = {"type": "play_title", "title": "the bear", "season": 3, "episode": 2, "kind": "series", "restart": False,
        "latest": False, "guess": False}
TAUGHT = [{"say": "Play the Bare season three", "do": {"type": "play_title", "title": "The Bear", "season": 3}}]


def entry(t: float, recording: str | None = "a.wav", action: dict = BEAR, ok: bool | None = True, **extra) -> dict:
    e = {"t": t, "source": "tv", "heard": "play the bear season 3 episode 2", "type": action["type"],
         "via": "pattern", "action": action, "ok": ok, **extra}
    return e | ({"recording": recording} if recording else {})


def test_expected_trusts_only_what_worked_and_wasnt_marked_wrong():
    want = {k: v for k, v in BEAR.items() if k not in ("guess", "kind")}     # unless a label names them
    assert V.expected(entry(1), TAUGHT) == (want, "history")
    assert V.expected(entry(1, wrong=True), TAUGHT)[0] is None
    assert V.expected(entry(1, ok=False), TAUGHT) == (None, "didn't work")
    assert V.expected(entry(1, action={"type": "none", "heard": "mm"}, ok=None), TAUGHT) == (None, "not understood")
    # someone adding it by hand vouches for it; the words heard are never expected
    assert V.expected(entry(1, action={"type": "none", "heard": "mm"}, ok=None), TAUGHT, asked=True) == \
        ({"type": "none"}, "history")
    assert V.expected(entry(1, ok=False), TAUGHT, asked=True)[1] == "history"


def test_expected_uses_what_was_taught():
    taught = entry(1, wrong=False, taught="play_title", heard="play the bare season three ")
    assert V.expected(taught, TAUGHT) == (TAUGHT[0]["do"], "taught")
    assert V.expected(taught, []) == (None, "taught, but no longer in learned.json")


def test_expected_sequence_steps_lose_the_unchecked_fields():
    seq = {"type": "sequence", "steps": [BEAR, {"type": "transport", "op": "seek_to", "percent": 50}]}
    expect, _ = V.expected(entry(1, action=seq), [])
    assert "guess" not in expect["steps"][0] and "kind" not in expect["steps"][0]
    assert expect["steps"][1]["percent"] == 50


def test_seed_labels_new_recordings_and_says_why_not():
    entries = [entry(1, "a.wav"), entry(2, "b.wav", ok=False), entry(3, "c.wav", wrong=True), entry(4, None),
               entry(5, "gone.wav")]
    labels = []
    has_audio = lambda name: name != "gone.wav"    # noqa: E731
    done = V.seed(entries, [], labels, has_audio)
    assert [x["recording"] for x in labels] == ["a.wav"]
    assert labels[0]["exact"] is True and labels[0]["from"] == "history" and labels[0]["heard"]
    assert done == {"added": 1, "didn't work": 1, "marked wrong: teach it the right action first": 1,
                    "recording gone": 1}
    assert V.seed(entries, [], labels, has_audio)["labelled"] == 1    # a second time: nothing new
    assert len(labels) == 1


def test_seed_follows_a_later_mark_but_never_a_hand_edit():
    labels = []
    V.seed([entry(1, "a.wav"), entry(2, "b.wav")], [], labels)
    labels[1]["expect"] = {"type": "play_title", "title_any": ["bear"]}     # edited by hand
    later = [entry(1, "a.wav", wrong=False, taught="play_title", heard="Play the Bare season three"),
             entry(2, "b.wav", wrong=True)]
    done = V.seed(later, TAUGHT, labels)
    assert done == {"updated": 1, "labelled": 1}
    a, b = sorted(labels, key=lambda x: x["recording"])
    assert a["expect"] == TAUGHT[0]["do"] and a["from"] == "taught"
    assert b["expect"] == {"type": "play_title", "title_any": ["bear"]}
    V.seed([entry(1, "a.wav", wrong=True)], [], (fresh := []))
    assert fresh == []
    V.seed([entry(1, "a.wav")], [], fresh)
    assert V.seed([entry(1, "a.wav", wrong=True)], [], fresh) == {"removed": 1} and fresh == []


def test_seed_history_add_and_load_keep_the_audio(tmp_path):
    recordings, tests = tmp_path / "recordings", tmp_path / "tests"
    recordings.mkdir()
    (recordings / "a.wav").write_bytes(b"RIFF")
    history = tmp_path / "activity.jsonl"
    history.write_text("".join(json.dumps(e) + "\n" for e in (entry(1, "a.wav"), entry(2, "b.wav"))))
    labels = tests / "labels.jsonl"
    done = V.seed_history(labels, history, tmp_path / "learned.json", recordings)
    assert done == {"added": 1, "recording gone": 1}
    assert [x["recording"] for x in V.load(labels)] == ["a.wav"]
    assert (tests / "a.wav").stat().st_ino == (recordings / "a.wav").stat().st_ino     # hard-linked, not copied
    (recordings / "a.wav").unlink()                                   # the agent's rotation, or erasing the history
    assert V.audio("a.wav", tests, recordings) == tests / "a.wav"
    assert labels.stat().st_mode & 0o777 == 0o600
    # the panel's "Add to voice tests": a new label replaces the recording's old one
    new, _ = V.label(entry(1, "a.wav", action={"type": "transport", "op": "pause"}), [], asked=True, text=" pause ")
    assert new["text"] == "pause" and new["from"] == "panel"
    assert V.add(new, labels, recordings) is True
    assert [x["expect"] for x in V.load(labels)] == [{"type": "transport", "op": "pause"}]


def test_load_names_a_broken_line(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text('{"recording": "a.wav", "expect": {"type": "home"}}\n\n{"recording": "b.wav"}\n')
    with pytest.raises(ValueError, match=r"labels.jsonl:3"):
        V.load(path)
    path.write_text('{"recording": "a.wav", "expect": {"type": "home"}\nnot json\n')
    with pytest.raises(ValueError, match=r"labels.jsonl:1"):
        V.load(path)
    assert V.load(tmp_path / "none.jsonl") == []


def test_transcript_words_and_error_rate():
    assert V.words("¿Qué estoy viendo?") == V.words("que estoy viendo")
    assert V.words("Spider-Man, don't stop.") == ["spider", "man", "dont", "stop"]
    assert V.words("Season three, episode twenty-one") == V.words("season 3 episode 21") == \
        ["season", "3", "episode", "21"]
    assert V.words("salta treinta y cinco segundos") == ["salta", "35", "segundos"]
    assert V.words("play a movie") == ["play", "a", "movie"] and V.words("treinta y") == ["30", "y"]
    assert V.wer("play the bear season three", "Play the Bear, season three.") == 0
    assert V.wer("play the bear season three", "play the bare season three") == pytest.approx(0.2)
    assert V.wer("pause", "") == 1 and V.wer("pause", "please pause") == 1


def result(ok: bool, transcript: str = "pause", text_ok: bool | None = None, **extra) -> dict:
    r = {"transcript": transcript, "ok": ok, "kind": True, "via": "phrase", "got": {"type": "transport", "op": "pause"},
         "expect": {"type": "transport", "op": "pause"}, "stt_ms": 100, "decide_ms": 1}
    return r | ({"text_ok": text_ok, "wer": 0.0 if text_ok else 0.5} if text_ok is not None else {}) | extra


def test_compare_finds_regressions_fixes_and_changed_transcripts():
    before = {"results": {"a.wav": {"local": result(True), "voxtral": result(True)},
                          "b.wav": {"local": result(False, "pours")},
                          "c.wav": {"local": result(True, "play the bear", text_ok=True)},
                          "d.wav": {"local": result(True, "home")},
                          "e.wav": {"local": result(True)},
                          "f.wav": {"local": result(True)},
                          "gone.wav": {"local": result(True)}}}
    after = {"results": {"a.wav": {"local": result(False, "pass"), "voxtral": result(True)},
                         "b.wav": {"local": result(True, "pause")},
                         "c.wav": {"local": result(True, "play the bare", text_ok=False)},
                         "d.wav": {"local": result(True, "go home")},
                         "e.wav": {"local": result(False, expect={"type": "home"})},
                         "f.wav": {"local": result(False, "", error="ReadTimeout")},
                         "new.wav": {"local": result(True)}}}
    c = V.compare(before, after)
    assert [(x["recording"], x["what"]) for x in c["regressions"]] == [("a.wav", "action"), ("c.wav", "transcript")]
    assert [(x["recording"], x["what"]) for x in c["fixes"]] == [("b.wav", "action")]
    assert [x["recording"] for x in c["changed"]] == ["d.wav"]       # different words, still right
    assert [x["recording"] for x in c["relabelled"]] == ["e.wav"]    # the label changed: not the code's doing
    assert [x["recording"] for x in c["errors"]] == ["f.wav"]
    assert c["only_before"] == [("gone.wav", "local")] and c["only_after"] == [("new.wav", "local")]
    assert V.compare(before, before) == {k: [] for k in c}


def test_totals_per_speech_model():
    t = V.totals({"a.wav": {"local": result(True, text_ok=True), "heard": result(True)},
                  "b.wav": {"local": result(False, text_ok=False, via="llm", cost=0.0002)},
                  "c.wav": {"local": result(False, error="ConnectError")}})
    local = t["local"]
    assert (local["n"], local["ok"], local["errors"], local["texts"], local["text_ok"]) == (3, 1, 1, 2, 1)
    assert local["wer"] == pytest.approx(0.25) and local["via"] == {"phrase": 1, "llm": 1}
    assert local["cost"] == pytest.approx(0.0002) and t["heard"]["wer"] is None


def test_judge_label_exact_checks_every_field_the_label_doesnt_name():
    bear = A.PlayTitle(title="The Bear", season=3)
    assert bench_voice.judge_label(bear, {"type": "play_title", "title": "the bear", "season": 3}, True) == (True, True)
    # a season the label doesn't name: wrong when exact, fine when not
    assert bench_voice.judge_label(bear, {"type": "play_title", "title": "the bear"}, True) == (True, False)
    assert bench_voice.judge_label(bear, {"type": "play_title", "title": "the bear"}, False) == (True, True)
    assert bench_voice.judge_label(bear, {"type": "play_title", "title_any": ["bear"], "season": 3}, True)[1]
    assert bench_voice.judge_label(bear.model_copy(update={"guess": True}),
                                   {"type": "play_title", "title": "The Bear", "season": 3}, True)[1]
    series = bear.model_copy(update={"kind": "series"})          # film or series counts only when named
    assert bench_voice.judge_label(series, {"type": "play_title", "title": "The Bear", "season": 3}, True)[1]
    assert not bench_voice.judge_label(series, {"type": "play_title", "title": "The Bear", "season": 3,
                                                "kind": "movie"}, True)[1]
    assert bench_voice.judge_label(bear, {"type": "continue_watching"}, True) == (False, False)
    assert bench_voice.judge_label(A.Transport(op="pause"), {"type": "transport"}, True)[1]     # op: required


def test_judge_label_any_of_for_every_field():
    burns = A.PlayTitle(title="The Simpsons", episode_name="Mr. Burns gets shot")
    want = {"type": "play_title", "title_any": ["simpson"], "episode_name_any": ["burns"]}
    assert bench_voice.judge_label(burns, want, True) == (True, True)
    assert bench_voice.judge_label(burns.model_copy(update={"episode_name": "Homer's dream"}), want, True) == (True, False)
    start = {"type": "transport", "op": "seek_to", "seconds_any": [0, None]}       # "go to the start", either way
    assert bench_voice.judge_label(A.Transport(op="seek_to"), start, True)[1]
    assert bench_voice.judge_label(A.Transport(op="seek_to", seconds=0), start, True)[1]
    assert not bench_voice.judge_label(A.Transport(op="seek_to", seconds=1200), start, True)[1]


def test_judge_label_sequence_step_by_step():
    seq = A.Sequence(steps=[A.PlayTitle(title="Severance", episode=2), A.Transport(op="seek_to", percent=50)])
    want = {"type": "sequence", "steps": [{"type": "play_title", "title": "severance", "episode": 2},
                                          {"type": "transport", "op": "seek_to", "percent": 50}]}
    assert bench_voice.judge_label(seq, want, True) == (True, True)
    want["steps"][1]["percent"] = 40
    assert bench_voice.judge_label(seq, want, True) == (True, False)
    assert bench_voice.judge_label(seq, {"type": "sequence", "steps": want["steps"][:1]}, True) == (True, False)
    assert bench_voice.judge_label(seq, {"type": "sequence"}, True) == (True, True)
