"""Each command's whole trace (tracing.py): its steps recorded, timed and labelled, the big parts in the command's
own file (activity.py: kept, pruned, erased, exported and imported), the settings it ran with, the LLM's calls; and
the panel's rendering of an old record and a new one (panel/tvpanel/timeline.py, stdlib only)."""
import asyncio
import gzip
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import httpx
import pytest

from tv_agent import activity, tracing
from tv_agent import actions as A
from tv_agent import server
from tv_agent.decide.llm import Llm
from tv_agent.decide.router import Decision

PANEL = Path(__file__).resolve().parents[2] / "panel" / "tvpanel" / "timeline.py"


def load_timeline():
    spec = importlib.util.spec_from_file_location("timeline", PANEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_steps_go_into_the_commands_trace_and_nowhere_else():
    assert tracing.step("x", "y") is None                    # no command: nothing kept

    async def go():
        tr = tracing.Trace()
        tracing.TRACE.set(tr)
        tracing.step("scene", "passages", label="Passages sent", ms=12.4, method=tracing.method("local", "bm25"),
                     confidence=0.5, threshold=0.4, verdict="sent", at="clash", detail={"passages": "x" * 10})
        await asyncio.create_task(asyncio.sleep(0))          # a task started by the command writes there too
        steps, details = tr.close()
        tracing.step("late", "after")                        # once recorded: left out
        return steps, details, tr
    steps, details, tr = asyncio.run(go())
    (s,) = steps
    assert s["label"] == "Passages sent" and s["ms"] == 12 and isinstance(s["at"], int) and s["at_"] == "clash"
    assert s["method"] == {"kind": "local", "id": "bm25"} and s["verdict"] == "sent"
    assert details[s["detail"]] == {"passages": "x" * 10} and len(tr.steps) == 1


def test_the_llms_calls_are_in_the_trace_with_their_prompt_and_answer():
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"fits\": []}"}}],
                                         "usage": {"prompt_tokens": 1200, "completion_tokens": 20, "cost": 0.0002}})

    async def go():
        tr = tracing.Trace()
        tracing.TRACE.set(tr)
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            llm = Llm(http, "key", "deepseek/x", rules=None, reasoning="none")
            await llm._post({"response_format": {"type": "json_schema", "json_schema": {"name": "episodes"}},
                             "messages": [{"role": "user", "content": "which?"}]}, 3.0)
        return tr.close()
    steps, details = asyncio.run(go())
    (s,) = steps
    assert s["label"] == "Which episode fits" and s["method"] == {"kind": "llm", "id": "deepseek/x"}
    assert s["tokens_in"] == 1200 and s["cost"] == 0.0002 and "ms" in s
    assert details[s["detail"]] == {"messages": [{"role": "user", "content": "which?"}], "answer": "{\"fits\": []}"}


def test_a_command_is_recorded_with_its_whole_trace_settings_and_details(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(activity, "TRACES", tmp_path / "traces")
    svc = server.Service.__new__(server.Service)
    svc.cfg = types.SimpleNamespace(log_transcripts=True)
    svc.settings = lambda: {"versions": {"agent": "0.11.0"}, "player": "tvplayer"}
    d = Decision(A.FindScene(what="paul rides the worm"), "pattern", 1.0, "find_scene", [{"stage": "clean"}])
    d.run = tracing.Trace()
    tracing.TRACE.set(d.run)
    tracing.step("scene", "passages", label="Passages sent", detail={"passages": "51:25 Shai-Hulud…"})
    tracing.TRACE.set(None)
    svc.record("tv", "Go to the scene where Paul rides the worm", d, True, "To 51:25", 1200,
               {"stt_model": "assemblyai/u3", "stt_ms": 280, "stt_where": "hosted", "audio_s": 2.1})
    (e,) = activity.read(activity.FILE)
    assert [t.get("label") for t in e["trace"]] == [None, "Speech to text", "Passages sent"]
    assert e["trace"][1]["at"] == -280 and e["trace"][1]["method"] == {"kind": "stt", "id": "assemblyai/u3"}
    assert e["settings"]["player"] == "tvplayer"
    path = activity.TRACES / e["details"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert activity.details(e, activity.TRACES) == {"scene.passages.1": {"passages": "51:25 Shai-Hulud…"}}
    assert "Shai-Hulud" not in activity.FILE.read_text()      # the big parts stay out of the history


def test_details_are_pruned_erased_exported_and_imported(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "KEEP_DETAILS", 3)
    folder, history = tmp_path / "traces", tmp_path / "activity.jsonl"
    names = [activity.save_details(float(t), {"n": t}, folder) for t in range(5)]
    assert sorted(p.name for p in folder.iterdir()) == names[2:]          # the newest 3
    exported = {"t": 9.0, "heard": "x", "details_data": {"n": 9}}
    assert activity.merge([exported], history, folder) == 1
    (e,) = activity.read(history)
    assert "details_data" not in e and activity.details(e, folder) == {"n": 9}
    activity.erase(history, folder)
    assert activity.read(history) == [] and not list(folder.glob("*.json.gz"))
    assert activity.details({"details": "../../etc/passwd"}, folder) is None


def test_the_settings_snapshot_has_versions_and_what_changes_the_result_never_a_secret():
    svc = server.Service.__new__(server.Service)
    from tv_agent.config import Config
    svc.cfg = Config()
    svc.cfg.player = "tvplayer"
    svc._settings, svc._kodi_version = None, None
    svc.updates = types.SimpleNamespace(versions={"tv": "0.9.2", "tvplayer": "0.6.2"})
    svc.clients = types.SimpleNamespace(devices=[types.SimpleNamespace(app_version="0.2.2")])
    svc.agent = types.SimpleNamespace(tvplayer=types.SimpleNamespace(app=""), rules=types.SimpleNamespace(
        source="defaults", phrases={"pause": ({}, "pause"), "x": ({}, "learned")}))
    svc.stt_language = lambda: "es"
    svc.stt_vocabulary = lambda: ["The Bear", "Dune"]
    s = server.Service.settings(svc)
    assert s["versions"]["tv_app"] == "0.9.2" and s["versions"]["player"] == "0.6.2" and s["versions"]["phones"] == ["0.2.2"]
    assert s["speech"]["vocabulary"] == 2 and s["deciding"]["taught"] == 1 and s["languages"]["native"] == "spa"
    assert "key" not in json.dumps(s).lower() and "token" not in json.dumps(s).lower()


def test_the_episode_search_leaves_its_steps(monkeypatch, tmp_path):
    from test_finding import EPS, finder
    from tv_agent import finding
    monkeypatch.setattr(finding, "CACHE", tmp_path)
    lines = {"tt1:1:10": [(1000, "Chang, you dance like my ex-wife")]}
    me, _ = finder(monkeypatch, {"fits": [{"id": "tt1:1:10", "match": "exactly"}]}, lines)

    async def go():
        tr = tracing.Trace()
        tracing.TRACE.set(tr)
        await finding.episode(me, "tt1", "Community", EPS, "Mr. Chang dances with his ex-wife")
        return tr.close()
    steps, details = asyncio.run(go())
    labels = [s.get("label") for s in steps if s["stage"] == "episode"]
    assert labels == ["Episode asked for", "Words to look for", "Plots ranked", "Which fit", "Checked in its subtitles",
                      "Episode"]
    ranked = next(s for s in steps if s.get("label") == "Plots ranked")
    assert ranked["method"]["id"] == "BM25 over the plots" and ranked["top"][0]["id"] == "tt1:1:10"
    assert "tt1:1:10" in details[ranked["detail"]]["plots"]
    assert next(s for s in steps if s.get("label") == "Episode")["verdict"] == "play"


def test_the_panel_shows_a_new_record_whole_and_an_old_one_as_it_was():
    tl = load_timeline()
    old = {"t": 1.0, "trace": [{"stage": "clean", "text": "pon x"}, {"stage": "phrase", "matched": True}]}
    assert tl.timeline(old, False) == ""                     # the diagram above shows what it has
    older = {"t": 1.0, "trace": [{"stage": "title", "step": "picked", "title": "X", "at": 900, "ms": 5}]}
    assert "<b>Title</b>" in tl.timeline(older, False)        # its steps without labels get theirs
    new = {"t": 2.0, "details": "2.000.json.gz", "settings": {"versions": {"agent": "0.11.0", "commit": "abc"},
                                                             "player": "tvplayer", "speech": {"where": "hosted",
                                                             "hosted_model": "assemblyai/u3", "language": "es",
                                                             "vocabulary": 120}},
           "trace": [{"stage": "title", "step": "choice", "label": "Title chosen", "at": 900, "ms": 40,
                      "method": {"kind": "d1", "id": "d1"}, "confidence": 0.31, "threshold": 0.4,
                      "verdict": "listed (not sure)", "candidates": ["Dune 90", "Dune: Prophecy 80"]},
                     {"stage": "llm", "step": "moment", "label": "Where the moment is", "at": 3319, "ms": 445,
                      "method": {"kind": "llm", "id": "deepseek/v4"}, "detail": "llm.moment.7", "tokens_in": 2890}]}
    html_ = tl.timeline(new, True)
    assert "Title chosen" in html_ and "confidence 0.31 &lt; 0.4" in html_ and "listed (not sure)" in html_
    assert "&nbsp;LLM&nbsp;" in html_ and "&nbsp;d1&nbsp;" in html_ and "href='detail:llm.moment.7'" in html_
    assert "How this trace was made" in html_ and "Settings at the time" in html_ and "agent 0.11.0" in html_
    assert "details aren't kept" in tl.timeline(new, False) and "href='detail:" not in tl.timeline(new, False)
    assert json.loads(tl.as_json(new, {"llm.moment.7": {"answer": "x"}}))["details"]["llm.moment.7"]["answer"] == "x"
