"""Phase 0 gate G3: d1-3B (/v1/systemone) intent decisions and LFM2.5-1.2B JSON slot extraction on the 780M.

    .venv/bin/python scripts/bench_decide.py        # servers on 127.0.0.1:8091 (d1) and :8092 (LFM)

Prints accuracy and warm p50/p95 latency for each model over a small labelled set.
"""
import json
import statistics
import time

import httpx

D1 = "http://127.0.0.1:8091/v1/systemone"
LFM = "http://127.0.0.1:8092/v1/chat/completions"

# Static part first so the server's prompt cache can reuse it; the utterance goes last.
TV = {
    "device": "living-room TV. Apps: TizenTube (YouTube), Stremio (movies and series), Kodi (player), "
              "Twitch, Movistar+ (Spanish live TV), Prime Video.",
    "note": "Movies and series are played in Kodi from the user's Stremio library and addons.",
}

INTENTS = {
    "play_title": "start a specific movie, or a specific episode or season of a series",
    "continue_watching": "resume or continue a series or movie the user was already watching",
    "next_episode": "play the next episode of what is playing now",
    "open_in_stremio": "show a title's page in the Stremio app, without playing it",
    "youtube": "play or search for something on YouTube",
    "twitch": "watch a Twitch streamer or channel",
    "open_app": "open an app by name",
    "home": "go to the home screen",
    "transport": "pause, resume, stop, skip forward or back, or jump to a time",
    "volume": "change the volume or mute",
    "none": "not a TV command, or unclear",
}

CASES = [
    ("play the bear season three episode two", "play_title", True, False),
    ("put on dune part two", "play_title", True, True),
    ("i want to watch severance from the start", "play_title", False, True),
    ("continue the bear", "continue_watching", False, False),
    ("keep watching what i was watching", "continue_watching", False, False),
    ("next episode", "next_episode", False, False),
    ("show me the office in stremio", "open_in_stremio", False, False),
    ("play lofi hip hop on youtube", "youtube", False, False),
    ("search youtube for how to make paella", "youtube", False, False),
    ("put ibai on twitch", "twitch", False, False),
    ("open prime video", "open_app", False, False),
    ("go home", "home", False, False),
    ("pause", "transport", False, False),
    ("skip forward thirty seconds", "transport", False, False),
    ("turn it down a bit", "volume", False, False),
    ("what's the weather like", "none", False, False),
]

SLOTS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]},
        "season": {"type": ["integer", "null"]},
        "episode": {"type": ["integer", "null"]},
        "query": {"type": ["string", "null"]},
        "channel": {"type": ["string", "null"]},
        "app": {"type": ["string", "null"]},
        "seconds": {"type": ["integer", "null"]},
    },
    "required": ["title", "season", "episode", "query", "channel", "app", "seconds"],
    "additionalProperties": False,
}
SLOTS_SYSTEM = (
    "Extract fields from a voice command for a TV. title: a movie or series name. season/episode: numbers. "
    "query: what to search on YouTube. channel: a Twitch channel. app: an app name. seconds: a time amount in "
    "seconds. Use null for anything not said. Do not invent values."
)


QUESTIONS = {
    "intent": {"type": "choice", "instructions": "What does the user want the TV to do?", "criteria": INTENTS},
    "is_movie": {"type": "noul", "instructions": "Is the user asking for a movie (not a series)?"},
    "restart": {"type": "noul", "instructions": "Does the user want to start from the beginning?"},
}


def d1_request(text, only=None):
    questions = {k: v for k, v in QUESTIONS.items() if only is None or k in only}
    return {"state": {"tv": TV, "utterance": text}, "questions": questions}


# Per-intent slot schemas: the model only fills what the chosen intent needs.
TITLE = {"type": "object", "properties": {"title": {"type": "string"}, "season": {"type": ["integer", "null"]},
                                          "episode": {"type": ["integer", "null"]}},
         "required": ["title", "season", "episode"], "additionalProperties": False}
PER_INTENT = {
    "play_title": TITLE,
    "open_in_stremio": TITLE,
    "continue_watching": {"type": "object", "properties": {"title": {"type": ["string", "null"]}},
                          "required": ["title"], "additionalProperties": False},
    "youtube": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                "additionalProperties": False},
    "twitch": {"type": "object", "properties": {"channel": {"type": "string"}}, "required": ["channel"],
               "additionalProperties": False},
    "open_app": {"type": "object", "properties": {"app": {"enum": ["TizenTube", "Stremio", "Kodi", "Twitch",
                                                                   "Movistar+", "Prime Video"]}},
                 "required": ["app"], "additionalProperties": False},
}
FEW_SHOT = [
    ("play breaking bad season two episode five", {"title": "Breaking Bad", "season": 2, "episode": 5}),
    ("put on the matrix", {"title": "The Matrix", "season": None, "episode": None}),
    ("continue watching", {"title": None}),
    ("find cat videos on youtube", {"query": "cat videos"}),
    ("put auronplay on twitch", {"channel": "auronplay"}),
]
FEW_SHOT_SYSTEM = ("Copy the requested fields out of a TV voice command as JSON. Titles and names are written as "
                   "said, with normal capitals; numbers as digits. Use null for anything not said.")


def slots_messages(text):
    msgs = [{"role": "system", "content": FEW_SHOT_SYSTEM}]
    for q, a in FEW_SHOT:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": json.dumps(a)}]
    return msgs + [{"role": "user", "content": text}]


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]


def main():
    c = httpx.Client(timeout=60)
    print(c.get("http://127.0.0.1:8091/v1/models").json()["data"][0].get("architecture"))

    # Cold: first request of each utterance. Warm: the same request again (prefix cached).
    cold, warm, ok = [], [], 0
    for text, want, movie, restart in CASES:
        for i in range(4):
            t = time.perf_counter()
            r = c.post(D1, json=d1_request(text)).json()
            ms = (time.perf_counter() - t) * 1000
            (cold if i == 0 else warm).append(ms)
        a = r["answers"]
        got = a["intent"]["choice"]
        ok += got == want
        print(f"  {'OK ' if got == want else 'BAD'} {text!r:48} -> {got:18} conf {a['intent']['confidence']:.2f}"
              f"  movie {a['is_movie']['noul']:.2f}  restart {a['restart']['noul']:.2f}  {ms:.0f} ms")
    print(f"d1-3B intent {ok}/{len(CASES)}; first request p50 {statistics.median(cold):.0f} ms p95 "
          f"{pct(cold, .95):.0f}; repeated p50 {statistics.median(warm):.0f} ms p95 {pct(warm, .95):.0f}; "
          f"{r['usage']['input_tokens']} input tokens")

    times = []
    for text, *_ in CASES:
        t = time.perf_counter()
        r = c.post(D1, json=d1_request(text, only={"intent"})).json()
        times.append((time.perf_counter() - t) * 1000)
    print(f"d1-3B intent question only: p50 {statistics.median(times):.0f} ms p95 {pct(times, .95):.0f} ms, "
          f"{r['usage']['input_tokens']} input tokens")

    times = []
    for text, want, *_ in CASES:
        schema = PER_INTENT.get(want)
        if schema is None:
            continue   # transport, volume, home, next, none: grammar or no slots
        body = {"messages": slots_messages(text), "temperature": 0, "max_tokens": 48,
                "response_format": {"type": "json_schema", "json_schema": {"name": want, "schema": schema}}}
        t = time.perf_counter()
        r = c.post(LFM, json=body).json()
        times.append((time.perf_counter() - t) * 1000)
        print(f"  {text!r:48} -> {r['choices'][0]['message']['content']}  {times[-1]:.0f} ms")
    print(f"LFM2.5-1.2B per-intent slots p50 {statistics.median(times):.0f} ms p95 {pct(times, .95):.0f} ms")


if __name__ == "__main__":
    main()
