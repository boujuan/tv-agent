"""Chains: reading one (decide/chain.py, the router), its fixed shape (actions.chain_problem), the LLM's proposal
checked, and running one (chain.py): the plan first, each step waiting for the last one's proof, stopping on a
failure, a timeout, "cancel" or Back."""
import asyncio
import json
import types

import pytest
from pydantic import ValidationError

from tv_agent import actions as A
from tv_agent import chain
from tv_agent.agent import Outcome
from tv_agent.decide import chain as reading
from tv_agent.decide import router
from tv_agent.decide import rules as R
from tv_agent.state import NowPlaying, State
from tv_agent.tv import tvplayer

BEAR = A.PlayTitle(title="the bear", season=2, episode=5, kind="series")


class NoD1:
    where = "local"

    async def choice(self, *args):
        raise router.httpx.ConnectError("off")


class FakeLlm:
    """Answers the chain question with `steps`, and a single command with `single`; counts the questions."""
    model = "fake/llm"

    def __init__(self, steps=None, single=None):
        self.steps, self.single, self.asked, self.last_usage = steps, single, [], {}

    async def _post(self, body, timeout):
        self.asked.append("chain")
        return {"choices": [{"message": {"content": json.dumps({"steps": self.steps})}}], "usage": {}}

    async def answer(self, said, context):
        self.asked.append("single")
        return True, self.single


def _router(llm=None):
    rules = R.build(R.defaults_raw(), "defaults")
    rules.fallback_intent = "none"
    return router.Router(NoD1(), rules, llm)


def decide(text, llm=None):
    return asyncio.run(_router(llm).decide(text))


# ---- reading a chain: rules first ----

@pytest.mark.parametrize("text,steps", [
    ("pon el capítulo 5 de la segunda temporada de The Bear y vete al minuto 12",
     [BEAR, A.Transport(op="seek_to", seconds=720)]),
    ("play Dune Part Two and go to the scene where Paul rides the worm",
     [A.PlayTitle(title="dune part two"), A.FindScene(what="paul rides the worm")]),
    ("pon Oppenheimer desde el principio con subtítulos en español",
     [A.PlayTitle(title="oppenheimer", restart=True), A.Tracks(subtitles="spa")]),
    ("pon the bear, vete al minuto 12 y pon subtítulos en inglés",
     [A.PlayTitle(title="the bear"), A.Transport(op="seek_to", seconds=720), A.Tracks(subtitles="eng")]),
    ("play the bear season 2 episode 5 from minute 12", [BEAR, A.Transport(op="seek_to", seconds=720)]),
    ("pon the bear desde el minuto 12", [A.PlayTitle(title="the bear"), A.Transport(op="seek_to", seconds=720)]),
    ("play oppenheimer in english with spanish subtitles",
     [A.PlayTitle(title="oppenheimer"), A.Tracks(audio="eng"), A.Tracks(subtitles="spa")]),
    ("play the bear with subtitles", [A.PlayTitle(title="the bear"), A.Tracks(subtitles="on")]),   # on, not toggle
    ("pon la casa de papel y salta la intro", [A.PlayTitle(title="la casa de papel"), A.Skip(what="intro")]),
    ("pon romeo y julieta y pausa", [A.PlayTitle(title="romeo y julieta"), A.Transport(op="pause")]),
    ("pon the bear y ve al minuto treinta y cinco",
     [A.PlayTitle(title="the bear"), A.Transport(op="seek_to", seconds=2100)]),
    ("play the bear and skip to an hour and a half",
     [A.PlayTitle(title="the bear"), A.Transport(op="seek_to", seconds=5400)]),
    ("go to the second episode of sevence and go to the middle of it",
     [A.PlayTitle(title="sevence", episode=2, kind="series"), A.Transport(op="seek_to", percent=50)]),
    ("play the bear and turn it up and pause",
     [A.PlayTitle(title="the bear"), A.Volume(op="up"), A.Transport(op="pause")]),
])
def test_the_rules_read_a_chain(text, steps):
    d = decide(text)
    assert d.via == "chain" and d.action == A.Sequence(steps=steps) and not d.note


@pytest.mark.parametrize("text", [
    "play pride and prejudice", "play tom and jerry", "pon romeo y julieta",     # a title with "and" is one title
    "skip the intro and the recap",
    "pon el volumen al treinta y cinco", "ve al minuto treinta y cinco", "skip to an hour and a half",   # numbers
    "pausa y luego", "para cuando acabe este, que estoy cansado",
    "the bear and pause",                       # a bare name: no play verb, no episode
    "play lost in translation", "play dances with wolves", "play the bear, season 2",
    "stop and go home",                         # a fixed phrase
])
def test_what_isnt_a_chain(text):
    d = decide(text)
    assert d.action.type != "sequence" and not d.note, (d.action, d.note)


@pytest.mark.parametrize("text,first,left", [
    ("pon the bear y apaga la tele cuando acabe", A.PlayTitle(title="the bear"), "a sleep timer can't follow a play"),
    ("play the bear and go to minute 12 and go to the scene where carmy cries", A.PlayTitle(title="the bear"),
     "two jumps"),
    ("play the bear and go to minute 12, turn it up and pause", A.PlayTitle(title="the bear"),
     "more than three steps"),
    ("play the bear in english with spanish subtitles from minute 12", A.PlayTitle(title="the bear"),
     "more than three steps"),
    ("play the bear and go home", A.PlayTitle(title="the bear"), "going home can't follow a play"),
    ("play the bear and skip forward 5 minutes", A.PlayTitle(title="the bear"), "a skip forward can't follow"),
    ("pause and go to minute 12", A.Transport(op="pause"), "a chain starts by playing something"),
])
def test_commands_that_cant_be_chained_run_the_first_alone_and_say_why(text, first, left):
    d = decide(text)
    assert d.action == first and left in d.note and d.note.startswith("Not done: “")


def test_the_llm_proposes_a_chain_only_when_the_rules_find_none_and_it_sounds_like_one():
    steps = [{"action": "play_title", "title": "Dune: Part Two"}, {"action": "find_scene", "query": "Paul rides"}]
    llm = FakeLlm([{**dict.fromkeys(reading.SLOTS), **s} for s in steps])
    d = decide("play dune part two and show me the bit with the worm ride", llm)
    assert d.via == "chain" and llm.asked == ["chain"]
    assert d.action == A.Sequence(steps=[A.PlayTitle(title="Dune: Part Two"), A.FindScene(what="Paul rides")])
    for text in ("subtítulos en español", "pon la casa de papel", "play the bear and go to minute 12"):
        llm = FakeLlm(single=A.Help())
        decide(text, llm)
        assert "chain" not in llm.asked, text           # the rules know them: no chain question


def test_a_refused_llm_chain_plays_its_first_step_alone_with_a_note():
    steps = [{"action": "play_title", "title": "Dune"}, {"action": "turn_off_after_this"}]
    llm = FakeLlm([{**dict.fromkeys(reading.SLOTS), **s} for s in steps])
    d = decide("play dune and then switch everything off when it ends", llm)
    assert d.action == A.PlayTitle(title="Dune") and "turn off after this can't follow a play" in d.note
    assert llm.asked == ["chain"]


# ---- the chain's fixed shape ----

@pytest.mark.parametrize("steps,why", [
    ([A.Volume(op="up"), A.PlayTitle(title="x")], "a chain starts by playing something"),
    ([A.PlayTitle(title="x", guess=True), A.Transport(op="pause")], "only a guess"),
    ([A.PlayTitle(title="x"), A.OpenApp(app="youtube")], "an app can't follow a play"),
    ([A.PlayTitle(title="x"), A.Timer(minutes=30)], "a sleep timer can't follow a play"),
    ([A.PlayTitle(title="x"), A.Transport(op="stop")], "stop can't follow a play"),
    ([A.PlayTitle(title="x"), A.Binge(on=True)], "binge mode can't follow a play"),
    ([A.PlayTitle(title="x"), A.PlayTitle(title="y")], "another title can't follow a play"),
    ([A.PlayTitle(title="x"), A.Skip(what="credits")], "skipping the credits can't follow a play"),
    ([A.PlayTitle(title="x"), A.Transport(op="pause"), A.Volume(op="up")], "a pause comes last"),
    ([A.PlayTitle(title="x"), A.FindScene(what="a"), A.Transport(op="seek_to", seconds=1)], "two jumps"),
    ([A.PlayTitle(title="x"), A.Tracks(audio="spa"), A.Tracks(audio="eng")], "the audio changed twice"),
    ([A.PlayTitle(title="x"), A.Transport(op="pause"), A.Transport(op="pause"), A.Volume(op="up")],
     "more than three steps"),
])
def test_the_shape_of_a_chain_is_fixed(steps, why):
    assert why in A.chain_problem(steps)
    with pytest.raises(ValidationError):
        A.Sequence(steps=steps)
    with pytest.raises(ValidationError):                # the same through the API (POST /v1/action)
        A.parse({"type": "sequence", "steps": [s.model_dump() for s in steps]})


def test_a_language_setting_isnt_a_chain_step():
    assert A.chain_problem([A.PlayTitle(title="x"), A.Tracks(audio="spa", keep=True)]) == "a setting can't be chained"
    assert A.chain_problem([A.PlayTitle(title="x"), A.Tracks(subtitles="foreign")]) == "a setting can't be chained"


def test_language_steps_can_be_switched_off(monkeypatch):
    steps = [A.PlayTitle(title="x"), A.Tracks(subtitles="spa")]
    assert A.chain_problem(steps) is None
    monkeypatch.setattr(A, "LANGUAGE_STEPS", False)
    assert A.chain_problem(steps) == "language changes can't be chained yet"
    assert A.chain_problem([A.PlayTitle(title="x"), A.Transport(op="pause")]) is None


# ---- the LLM's proposal, checked ----

def steps_of(*steps):
    return {"steps": [{**dict.fromkeys(reading.SLOTS), **s} for s in steps]}


RULES = R.build(R.defaults_raw(), "defaults")
SAID = "pon the bear y vete al minuto doce"


ALL = SAID + ", sube el volumen, ve a la escena donde llora, pon subtítulos y pausa"


@pytest.mark.parametrize("answer,first,why", [
    (steps_of({"action": "play_title", "title": "The Bear"}, {"action": "teleport"}), "play_title", "doesn't know"),
    ({"steps": [{**dict.fromkeys(reading.SLOTS), "action": "play_title", "title": "The Bear"},
                {**dict.fromkeys(reading.SLOTS), "action": "seek_to", "seconds": 720, "mood": "x"}]},
     "play_title", "field the chain doesn't know"),
    (steps_of({"action": "play_title", "title": "The Bear", "seconds": 720}, {"action": "pause"}),
     None, "doesn't take"),                     # a step's own field in the play: two steps merged into one
    (steps_of({"action": "play_title", "title": "The Bear"}, {"action": "pause"}), "play_title", "wasn't asked"),
    (steps_of({"action": "volume_up"}, {"action": "play_title", "title": "The Bear"}), None, "starts by playing"),
    (steps_of({"action": "play_title", "title": "The Bear"}, {"action": "seek_to", "seconds": 720},
              {"action": "find_scene", "query": "x"}), "play_title", "two jumps"),
    (steps_of({"action": "play_title", "title": "The Bear"}, {"action": "seek_to", "seconds": 720},
              {"action": "volume_up"}, {"action": "mute"}), "play_title", "more than three"),
    (steps_of({"action": "play_title", "title": "The Bear"}, {"action": "subtitles", "language": "klingon"}),
     "play_title", "doesn't hold up"),
    ({"steps": [], "extra": 1}, None, "shape"),
])
def test_any_flaw_rejects_the_whole_llm_chain(answer, first, why):
    got = reading.read(answer, SAID if why == "wasn't asked" else ALL, [], RULES)
    assert why in got.problem and (got.action.type if got.action else None) == first


def test_a_step_folded_into_a_one_step_play_isnt_lost_silently():
    got = reading.read(steps_of({"action": "play_title", "title": "The Bear", "seconds": 720}), SAID, [], RULES)
    assert got.action == A.PlayTitle(title="The Bear") and got.problem == "the play came with seconds"


def test_a_sound_llm_chain_and_a_single_answer():
    got = reading.read(steps_of({"action": "play_title", "title": "The Bear", "season": 2, "episode": 5},
                                {"action": "seek_to", "seconds": 720}), SAID, [], RULES)
    assert not got.problem and got.action == A.Sequence(steps=[
        A.PlayTitle(title="The Bear", season=2, episode=5), A.Transport(op="seek_to", seconds=720)])
    got = reading.read(steps_of({"action": "volume_up"}), "turn it up", [], RULES)
    assert not got.problem and got.action == A.Volume(op="up")


# ---- running a chain ----

class Player:
    """TV Agent Player as the chain sees it."""

    def __init__(self):
        self.up, self.listeners, self.last, self.item, self.pos, self.playing = True, [], {}, None, None, True
        self.stopped = False
        self.subtitle = None
        self._coming = None

    def now(self):
        return None if self.pos is None else (self.pos, 3_600_000, 1.0 if self.playing else 0.0)

    async def position(self, pid):
        if self.pos is None:
            raise tvplayer.PlayerError("nothing is open in the player")
        return self.now()

    async def tracks(self, pid):
        sub = {"language": self.subtitle} if self.subtitle else {}
        return {"currentaudiostream": {"language": "en"}, "currentsubtitle": sub, "subtitleenabled": bool(sub)}

    async def stop(self, pid):
        self.stopped, self.pos = True, None

    def emit(self, msg):
        for listener in list(self.listeners):
            listener(msg)


class Agent:
    """Just enough of agent.Agent for chain.run: what each action does to the fake player, and what was done."""

    def __init__(self, started="tt1:2:5", hang=(), fail=()):
        self.tvplayer, self.state, self.cfg = Player(), State(), types.SimpleNamespace(player="tvplayer")
        self.done, self.shown, self.chain, self._verify_task, self.heard = [], [], None, None, []
        self.started, self.hang, self.fail = started, hang, fail
        self.went_home = False

    async def notify(self, text):
        self.shown.append(text)

    async def execute(self, a, heard=""):
        self.done.append(a.type)
        self.heard.append(heard)
        if a.type in self.hang:
            await asyncio.Event().wait()
        if a.type in self.fail:
            return Outcome(False, f"{a.type} failed")
        match a:
            case A.PlayTitle():
                self.state.now_playing = NowPlaying("tt1", "series", self.started, "The Bear", "tvplayer")
                self.tvplayer.item, self.tvplayer.pos = f"{self.started}#1", 0
                self.tvplayer.last = {"first_frame": {"ms": 900}}
                return Outcome(True, "Playing The Bear · S2E5", {"stages": {"first_frame_ms": 900}})
            case A.Transport(op="seek_to"):
                self.tvplayer.pos = a.seconds * 1000
                return Outcome(True, f"To {chain.clock(a.seconds * 1000)}")
            case A.Transport(op="pause"):
                self.tvplayer.playing = False
                return Outcome(True, "Pause")
            case A.Tracks():
                self.tvplayer.subtitle = a.subtitles
                return Outcome(True, "Spanish subtitles")
        return Outcome(True, a.type)

    async def _player_id(self):
        return tvplayer.PID if self.tvplayer.now() is not None else None

    def _ctl(self, pid):
        return self.tvplayer

    async def _just_status(self):
        return None

    async def _home_unless_playing(self):
        self.went_home = True


def run(agent, *steps, during=None):
    async def go():
        task = asyncio.create_task(chain.run(agent, A.Sequence(steps=list(steps)), "what was said"))
        if during:
            await during(agent, task)
        return await task
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(chain, "WAIT", {k: 0.5 for k in chain.WAIT})
    monkeypatch.setattr(chain, "PLAY_WAIT", 1.0)


def test_the_plan_shows_first_then_each_step_once_the_last_is_done():
    agent = Agent()
    ok, say, details = run(agent, BEAR, A.Transport(op="seek_to", seconds=720), A.Transport(op="pause"))
    assert ok and agent.done == ["play_title", "transport", "transport"]
    assert agent.shown[0] == "The Bear S2E5, then 12:00, then pause · “cancel” or Back stops it"
    assert say == "Playing The Bear · S2E5 · To 12:00 · Pause" and agent.tvplayer.pos == 720_000
    assert details["chain"] == ["The Bear S2E5", "12:00", "pause"] and details["stages"]   # the start's record kept
    assert agent.heard[0] == "what was said"            # the play gets the sentence (its loading screen quotes it)


def test_a_failed_step_stops_the_chain_and_says_which_and_why():
    agent = Agent(fail=("find_scene",))
    ok, say, _ = run(agent, BEAR, A.FindScene(what="carmy cries"), A.Transport(op="pause"))
    assert not ok and agent.done == ["play_title", "find_scene"]          # the pause never ran
    assert say == "Step 2 (the scene: “carmy cries”): find_scene failed · not done: pause"


def test_a_failed_start_runs_nothing_after_it():
    agent = Agent(fail=("play_title",))
    ok, say, _ = run(agent, BEAR, A.Transport(op="seek_to", seconds=720))
    assert not ok and agent.done == ["play_title"] and say.startswith("Step 1 (The Bear S2E5): play_title failed")


def test_the_wrong_episode_stops_the_chain_before_the_jump():
    agent = Agent(started="tt1:2:6")
    ok, say, _ = run(agent, BEAR, A.Transport(op="seek_to", seconds=720))
    assert not ok and agent.done == ["play_title"] and "it started S2E6 instead" in say


def test_a_step_that_takes_too_long_stops_the_chain():
    agent = Agent(hang=("volume",))
    ok, say, _ = run(agent, BEAR, A.Volume(op="up"), A.Transport(op="pause"))
    assert not ok and agent.done == ["play_title", "volume"]
    assert say == "Step 2 (volume up) didn't finish within 0 s · not done: pause"


def test_a_start_that_takes_too_long_is_abandoned():
    agent = Agent(hang=("play_title",))
    ok, say, _ = run(agent, BEAR, A.Transport(op="seek_to", seconds=720))
    assert not ok and "didn't start within" in say and agent.tvplayer.stopped      # its loading screen closed


def test_a_step_that_breaks_stops_the_chain_and_says_which():
    agent = Agent()

    async def broken(what):
        raise tvplayer.PlayerError("the player went away")
    agent._skip_when_it_comes = broken
    ok, say, _ = run(agent, BEAR, A.Skip(what="intro"), A.Transport(op="pause"))
    assert not ok and say == "Step 2 (skip the intro): it failed (PlayerError) · not done: pause"


def test_a_jump_past_the_end_fails():
    agent = Agent()
    ok, say, _ = run(agent, BEAR, A.Transport(op="seek_to", seconds=4 * 3600))
    assert not ok and "4:00:00 is past the end (1:00:00)" in say and agent.done == ["play_title"]


def test_cancel_stops_what_is_left():
    agent = Agent(hang=("find_scene",))
    said = []

    async def cancel_it(agent, task):
        while agent.done != ["play_title", "find_scene"]:
            await asyncio.sleep(0.01)
        said.append(await chain.cancel(agent))
    ok, say, _ = run(agent, BEAR, A.FindScene(what="carmy cries"), A.Transport(op="pause"), during=cancel_it)
    assert not ok and say == "Stopped: you said cancel · not done: the scene: “carmy cries”, pause"
    assert said == [(True, "Cancelled · not done: the scene: “carmy cries”, pause")]
    assert not agent.tvplayer.stopped                   # what plays goes on


def test_cancel_during_the_start_closes_its_loading_screen():
    agent = Agent(hang=("play_title",))
    agent.tvplayer.pos = None

    async def cancel_it(agent, task):
        while not agent.done:
            await asyncio.sleep(0.01)
        await chain.cancel(agent)
    ok, say, _ = run(agent, BEAR, A.Transport(op="seek_to", seconds=720), during=cancel_it)
    assert not ok and say.startswith("Stopped: you said cancel") and agent.tvplayer.stopped


def test_cancel_with_nothing_on_its_way_says_so_and_touches_nothing():
    agent = Agent()
    agent.tvplayer.pos, agent.tvplayer.last = 600_000, {"pos": {"pos": 600_000}}     # a title plays
    assert asyncio.run(chain.cancel(agent)) == (False, "Nothing to cancel") and not agent.tvplayer.stopped


def test_cancel_after_a_replaced_start_closes_its_loading_screen():
    """server.start cancelled a start still finding streams; the player had kept an item it never loaded."""
    agent = Agent()
    agent.tvplayer.pos, agent.tvplayer.last, agent.tvplayer._coming = 0, {}, ("tt2", "tt2#1")

    async def go():
        chain.INTERRUPTED.set(True)
        return await chain.cancel(agent)
    assert asyncio.run(go()) == (True, "Cancelled") and agent.tvplayer.stopped


def test_cancel_leaves_what_plays_behind_the_loading_screen():
    agent = Agent()
    agent.tvplayer.pos, agent.tvplayer.last, agent.tvplayer._coming = 600_000, {"pos": {}}, ("tt2", "tt2#1")
    cleared = []

    async def clear_loading():
        cleared.append(True)
    agent.tvplayer.clear_loading = clear_loading
    assert asyncio.run(chain.cancel(agent)) == (True, "Cancelled") and cleared and not agent.tvplayer.stopped


def test_back_in_the_player_stops_the_chain():
    agent = Agent(hang=("transport",))

    async def back(agent, task):
        while "transport" not in agent.done:
            await asyncio.sleep(0.01)
        agent.tvplayer.emit({"t": "closed", "why": "back", "item": agent.tvplayer.item, "pos": 5000})
    ok, say, _ = run(agent, BEAR, A.Transport(op="seek_to", seconds=720), during=back)
    assert not ok and say == "Stopped: Back was pressed · not done: 12:00"


def test_the_player_closing_while_it_starts_stops_the_chain():
    agent = Agent(hang=("play_title",))

    async def closed(agent, task):
        await asyncio.sleep(0.3)
        agent.tvplayer.up = False
    ok, say, _ = run(agent, BEAR, A.Transport(op="seek_to", seconds=720), during=closed)
    assert not ok and say.startswith("Stopped: the player was closed")


def test_another_command_cancels_the_chain_without_touching_the_player():
    agent = Agent(hang=("transport",))

    async def replaced(agent, task):
        while "transport" not in agent.done:
            await asyncio.sleep(0.01)
        task.cancel()                                   # server.start: a new command replaces it
    with pytest.raises(asyncio.CancelledError):
        run(agent, BEAR, A.Transport(op="seek_to", seconds=720), during=replaced)
    assert not agent.tvplayer.stopped and not agent.went_home and agent.chain.why == "another command came"


def test_a_language_step_waits_for_the_episode_check_and_is_checked():
    agent = Agent()
    order = []

    async def verdict():
        await asyncio.sleep(0.2)
        order.append("verdict")

    async def with_check(agent, task):
        agent._verify_task = asyncio.create_task(verdict())
    real = agent.execute

    async def execute(a, heard=""):
        order.append(a.type)
        return await real(a, heard)
    agent.execute = execute
    ok, say, _ = run(agent, BEAR, A.Tracks(subtitles="spa"), during=with_check)
    assert ok and order == ["play_title", "verdict", "tracks"]


def test_a_language_that_doesnt_take_fails_the_step():
    agent = Agent()
    real = agent.execute

    async def execute(a, heard=""):
        out = await real(a, heard)
        agent.tvplayer.subtitle = None if a.type == "tracks" else agent.tvplayer.subtitle
        return out
    agent.execute = execute
    ok, say, _ = run(agent, BEAR, A.Tracks(subtitles="spa"))
    assert not ok and say.startswith("Step 2 (Spanish subtitles) didn't finish within")


# ---- a scene: placed by the line quoted, weak matches refused ----

CUES = [(n * 1000, f"line number {n}") for n in range(1, 300)] + [(280_000, "Long live the fighters!")]
CUES += [(7 * 60_000 + 20_000, "We don't kill. We never kill."), (500_000, "Then we'll eat"), (502_000, "what's left.")]
CUES.sort()


@pytest.mark.parametrize("at,line,how,why", [
    ("1:40", "line number 100", "guess", "the subtitles don't show it"),
    ("1:40", "line number 100", "", "no reason given"),
    ("1:40", "ok", "words", "no line to place it by"),
    ("1:40", "we ride the worm at dawn", "words", "isn't in the subtitles"),               # made up
])
def test_weak_scene_matches_are_refused(at, line, how, why):
    ms, said = chain.place_scene(at, line, how, CUES)
    assert ms is None and why in said


def test_a_scene_is_placed_by_the_line_quoted_not_the_models_arithmetic():
    assert chain.place_scene("1:41", "Line number 101!", "words", CUES) == (101_000, "")
    assert chain.place_scene("1:39", "<i>line number 99</i>", "around", CUES) == (99_000, "")
    # 2026-10-24, Pluribus: the model said 26400 s for a line listed at 7:20; the line is what counts
    assert chain.place_scene("7:20:00", "We don't kill.", "words", CUES) == (440_000, "")
    assert chain.place_scene("", "Then we'll eat what's left.", "around", CUES) == (500_000, "")   # two cues


def test_a_line_said_in_two_places_needs_its_time():
    cues = [(60_000, "Run!"), (61_000, "Get to the choppa now"), (900_000, "Get to the choppa now")]
    assert chain.place_scene("15:00", "Get to the choppa now", "words", cues) == (900_000, "")
    ms, why = chain.place_scene("3:00", "Get to the choppa now", "words", cues)
    assert ms is None and why == "that line is said in more than one place"


@pytest.mark.parametrize("text,cut", [
    ("paul rides the", True), ("Mr. Chang dances with", True), ("Play Dune: Part Two and go to the scene where "
                                                                 "Paul rides the", True),
    ("carol se entera de que", True), ("paul rides the worm", False),
    ("carol se entera de que comen carne humana", False), ("they kiss", False), ("", False),
])
def test_a_description_cut_off_while_it_was_said(text, cut):
    assert chain.cut_off(text) is cut


def test_a_start_made_later_in_the_background_writes_nothing_into_a_finished_record():
    """The next episode (binge), another copy: started from the context of the command that started the first one,
    whose record was already handed over (2026-10-24: "after … ended: KeyError", the next episode never started)."""
    from tv_agent import agent as agent_mod

    async def go():
        record = {"title": {"name": "Community"}}            # handed over: its "_t0" is gone
        agent_mod.START.set(record)
        agent_mod._step("start", "streams", 5)
        agent_mod._note_title(by="search")
        return record
    assert asyncio.run(go()) == {"title": {"name": "Community"}}


@pytest.mark.parametrize("text", ["Stop the video and go back to the menu", "Para y vuelve a menú",
                                  "para el vídeo y vuelve al menú", "stop and go home"])
def test_stop_and_home_said_together_is_one_stop(text):
    """Stop goes back to Home by itself: no note about Home not being done (a regression of the chains: the voice
    tests of 2026-10-23 had these as two commands)."""
    d = decide(text)
    assert d.action == A.Transport(op="stop") and not d.note


def test_stop_in_another_app_goes_home_too():
    from tv_agent import agent as agent_mod
    from tv_agent.tv import apps, media
    did = []

    async def tv(fn, *args):
        did.append((fn, *args))

    async def front():
        return "com.google.android.youtube.tv"

    async def no_kodi():
        return None
    me = types.SimpleNamespace(tvplayer=types.SimpleNamespace(now=lambda: None), front_app=front, _kodi_player=no_kodi,
                               tv=tv)
    out = asyncio.run(agent_mod.Agent._transport(me, A.Transport(op="stop")))
    assert out.ok and did == [(media.dispatch, "stop"), (apps.home,)]


def test_a_start_stops_when_its_loading_screen_is_closed():
    """TV Agent Player 0.6.2: Back on a start's loading screen sends "closed" at 0 for the item it was loading."""
    from tv_agent.agent import Agent
    me = types.SimpleNamespace(_left=asyncio.Event(), tvplayer=types.SimpleNamespace(_coming=("tt1:2:5", "tt1:2:5#3-1")))
    cancelled = []

    async def start():
        try:
            await asyncio.sleep(30)                         # still finding streams
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def go():
        task = asyncio.create_task(Agent._unless_left(me, start()))
        await asyncio.sleep(0.01)
        Agent._left_loading(me, {"t": "closed", "item": "tt1:2:4#3-0", "pos": 0, "why": "back"})   # another start's
        await asyncio.sleep(0.01)
        assert not task.done()
        Agent._left_loading(me, {"t": "closed", "item": "tt1:2:5#3-1", "pos": 0, "dur": 0, "why": "back"})
        return await task
    out = asyncio.run(go())
    assert not out.ok and out.say == "Stopped: the player was closed before it started" and cancelled
