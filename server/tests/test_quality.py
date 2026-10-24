"""The Quality numbers (quality.py) from commands' traces, the nightly check against its baselines, and a command of
the history kept as a test (voicetests.keep_command, the panel's “This was right / wrong”)."""
import json

from tv_agent import quality as Q
from tv_agent import stt
from tv_agent import voicetests as V


def cmd(t: float, **extra) -> dict:
    return {"t": t, "source": "tv", "heard": "play the bear", "type": "play_title", "via": "pattern", "rule": "play",
            "ok": True, "stt_model": "whisper", "stt_ms": 400, "decide_ms": 20, "ms": 3000, **extra}


def step(stage: str, name: str, at: int, ms: int = 100, **extra) -> dict:       # ms since the command began
    return {"stage": stage, "step": name, "at": at, "ms": ms, **extra}


START = [step("start", "streams", 10_000, 800, kept=4), step("start", "first frame", 12_000, 2000)]
EPISODE = [step("episode", "asked", 5_000, 0, imdb="tt1439629", title="Community", wanted="chang dances"),
           step("episode", "plots", 6_000, 900), step("episode", "verdict", 7_000, 0, verdict="play",
                                                    episode="tt1439629:3:5")]
SCENE = [step("scene", "asked", 20_000, 0, video="tt1439629:3:5", title="Community", wanted="the dance"),
         step("scene", "jump", 23_000, 300, verdict="done", to="12:40")]


def test_each_step_right_asked_or_wrong_with_its_time():
    s = Q.steps_of(cmd(1, trace=START + EPISODE))
    assert s["speech"] == (True, 400) and s["decide"] == (True, 20)
    assert s["episode"] == (True, 2000)                 # from its first step's start to its last's end
    assert s["streams"] == (True, 800) and s["start"] == (True, 3000) and s["outcome"] == (True, 3420)
    listed = [step("episode", "verdict", 7_000, 0, verdict="list")]
    assert Q.steps_of(cmd(1, trace=listed))["episode"][0] is None
    wrong = Q.steps_of(cmd(1, wrong=True, trace=SCENE))
    assert wrong["decide"][0] is False and wrong["scene"][0] is False and wrong["outcome"][0] is False
    typed = Q.steps_of(cmd(1, stt_model=None, stt_ms=None, source="text"))
    assert "speech" not in typed and "start" not in typed       # no start traced: not counted


def test_summary_counts_deciders_models_costs_and_failures():
    llm = {"stage": "llm", "step": "call", "at": 3_000, "ms": 700, "cost": 0.002, "method": {"kind": "llm", "id": "m/x"}}
    entries = [cmd(1_000, trace=START + [llm], stt_cost=0.001),
               cmd(1_001, via="llm", rule="", ok=False, say="Nothing found for “The Baer” 3"),
               cmd(1_002, via="llm", rule="", ok=False, say="Nothing found for “Dune” 2"),
               cmd(90_000, wrong=True)]
    s = Q.summary(Q.pick(entries, sources={"tv"}))
    assert s["n"] == 4
    by = {x["key"]: x for x in s["steps"]}
    assert by["decide"]["n"] == 4 and by["decide"]["right"] == 3 and by["outcome"]["right"] == 1
    assert by["start"]["n"] == 1 and by["start"]["p50"] == 3000
    assert {x["key"]: (x["n"], x["right"]) for x in s["deciders"]} == {"pattern: play": (2, 1), "llm": (2, 0)}
    assert s["models"][0]["model"] == "speech: whisper" and s["models"][0]["calls"] == 4
    assert {m["model"]: m["cost"] for m in s["models"]}["m/x"] == 0.002
    assert sum(c for _, c in s["cost_by_day"]) == 0.003 and len(s["cost_by_day"]) == 2
    assert s["failures"][0] == {"reason": "Nothing found for “…” #", "n": 2, "example": 1_002}
    assert s["failures"][1]["reason"] == "Marked “that's wrong”"
    assert Q.pick(entries, since=50_000) == entries[3:] and Q.pick(entries, sources={"phone"}) == []


def test_nightly_compares_with_the_baseline_and_alerts_only_on_a_regression(tmp_path, monkeypatch):
    results = {"chains": {"rows": [{"text": "pause", "ok": True, "cost": 0.0},
                                   {"text": "play dune then go to 1:00", "ok": False, "cost": 0.01}]}}

    def bench(name, spec, out):
        out.write_text(json.dumps(results[name]))
        return results[name], 1.0
    monkeypatch.setattr(Q, "_bench", bench)
    first = Q.nightly("local", ["chains"], tmp_path)
    assert first["regressions"] == 0 and first["benches"]["chains"]["right"] == 1
    assert (tmp_path / "baselines" / "chains.json").exists() and Q.alert(tmp_path) is None

    results["chains"]["rows"] = [{"text": "pause", "ok": False, "cost": 0.0},
                                 {"text": "play dune then go to 1:00", "ok": True, "cost": 0.01},
                                 {"text": "new one", "ok": True}]
    monkeypatch.setattr(Q.time, "strftime", lambda f, *a: "2026-10-25-0430" if f == "%Y-%m-%d-%H%M" else "2026-10-25")
    run = Q.nightly("local", ["chains"], tmp_path)
    b = run["benches"]["chains"]
    assert (b["regressions"], b["fixes"], b["new"]) == (["pause"], ["play dune then go to 1:00"], ["new one"])
    assert run["regressions"] == 1 and run["cost"] == 0.01
    assert Q.alert(tmp_path)["run"] == "2026-10-25-0430.json"
    assert [r["file"] for r in Q.runs(tmp_path)][0] == "2026-10-25-0430.json"
    Q.seen(tmp_path)
    assert Q.alert(tmp_path) is None
    assert Q.accept("2026-10-25-0430.json", tmp_path) == ["chains"]
    again = Q.nightly("local", ["chains"], tmp_path)
    assert again["regressions"] == 0 and again["benches"]["chains"]["new"] == []


def test_a_bench_that_fails_is_shown_not_compared(tmp_path, monkeypatch):
    def broken(name, spec, out):
        raise ValueError("no output")
    monkeypatch.setattr(Q, "_bench", broken)
    run = Q.nightly("local", ["finding"], tmp_path)
    assert run["benches"]["finding"]["error"] == "ValueError: no output" and run["regressions"] == 0


def test_bench_results_as_cases():
    voice = {"results": {"a.wav": {"local": {"ok": True, "transcript": "pause", "cost": 0.001}}}}
    assert Q._cases("voice", voice) == {"a.wav (local)": {"ok": True, "got": "pause", "cost": 0.001}}
    finding = [{"row": 1, "run": "cold", "wanted": "the dance", "ok": False, "got": "none", "cost": 0.003},
               {"row": 2, "run": "cold", "wanted": "anything", "ok": None, "got": "ask"}]
    assert list(Q._cases("finding", finding)) == ["1. the dance (cold)"]


def test_a_command_kept_as_tests(tmp_path, monkeypatch):
    tests, recordings = tmp_path / "tests", tmp_path / "recordings"
    recordings.mkdir()
    (recordings / "a.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(V, "LABELS", tests / "labels.jsonl")
    monkeypatch.setattr(V, "SENTENCES", tests / "sentences.jsonl")
    monkeypatch.setattr(V, "FINDING", tests / "finding.jsonl")
    monkeypatch.setattr(stt, "RECORDINGS", recordings)
    e = cmd(1, recording="a.wav", trace=EPISODE + SCENE)
    expect = {"type": "play_title", "title": "Community"}
    # right: the recording, what it found
    assert V.keep_command(e, expect, " play community ", True) == ["a voice test", "an episode case", "a scene case"]
    label = V.load(V.LABELS)[0]
    assert label["text"] == "play community" and label["exact"] and (tests / "a.wav").exists()
    found = V.cases(V.FINDING)
    assert found[0]["expect"] == ["tt1439629:3:5"] and found[1]["expect"] == [740, 780]
    # wrong: what you say it should have found replaces the cases; typed, it's one of your sentences
    typed = cmd(2, trace=EPISODE + SCENE, source="text")
    assert V.keep_command(typed, expect, "pon community y luego el baile", False, "s3e6", "1:02:00") == \
        ["one of your sentences", "an episode case", "a scene case"]
    assert V.cases(V.SENTENCES)[0]["lang"] == "es"
    assert [c["expect"] for c in V.cases(V.FINDING)] == [["tt1439629:3:6"], [3700, 3740]]
    # only the moment was wrong, nothing to say what it should have done: just that case
    assert V.keep_command(cmd(3, trace=SCENE), None, "", False, None, "0:30") == ["a scene case"]
    assert V.keep_command(cmd(4, trace=SCENE), None, "", False) == []


def test_a_paired_phone_asks_for_the_numbers_and_the_alert(tmp_path, monkeypatch):
    from test_clients import call, paired, service      # the phone listener's helpers

    from tv_agent import activity, server
    from tv_agent import clients as C
    from tv_agent import config
    for name in ("CONFIG_DIR", "CONFIG_FILE", "CLIENTS_FILE", "TLS_CERT", "TLS_KEY"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    monkeypatch.setattr(activity, "FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(Q, "DIR", tmp_path / "quality")
    (tmp_path / "activity.jsonl").write_text(json.dumps(cmd(1e12, source="phone")) + "\n")
    (tmp_path / "quality").mkdir()
    (tmp_path / "quality" / "alert.json").write_text(json.dumps({"run": "x.json", "regressions": 2, "seen": False}))
    svc = service(tmp_path)
    token = paired(svc)
    gate = C.gateway(svc, server.local_app(svc))
    got = call(gate, "GET", "/v1/quality?days=0&sources=phone", token).json()
    assert got["summary"]["n"] == 1 and got["alert"]["regressions"] == 2 and got["runs"] == []
    assert call(gate, "GET", "/v1/quality?sources=tv", token).json()["summary"]["n"] == 0
    assert call(gate, "POST", "/v1/quality/seen", token).status_code == 200
    assert call(gate, "GET", "/v1/quality", token).json()["alert"] is None
    assert call(gate, "GET", "/v1/quality?days=x", token).status_code == 422
