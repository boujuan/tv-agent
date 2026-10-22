"""Generate eval/commands.jsonl: labelled TV commands covering every action, many ways of saying each, titles that
contain command words, numbers in every form, fillers, speech-to-text punctuation and known mishearings, and
sentences that aren't commands. Deterministic (fixed seed); each row is "dev" (for tuning rules and the model's
options) or "test" (held out, to check the tuning generalises).

    .venv/bin/python eval/build_commands.py        # rewrites eval/commands.jsonl
    .venv/bin/tv-agent eval --file eval/commands.jsonl [--split test]
"""
import hashlib
import json
import random
from pathlib import Path

OUT = Path(__file__).with_name("commands.jsonl")
rng = random.Random(20261022)

WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "twenty one",
         "twenty two", "twenty three", "twenty four"]
ORD = ["zeroth", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
       "eleventh", "twelfth"]

MOVIES = ["dune", "dune part two", "oppenheimer", "interstellar", "the godfather", "the matrix", "parasite",
          "inception", "barbie", "gladiator", "alien romulus", "the batman", "joker", "blade runner 2049",
          "ocean's eleven", "the dark knight", "the holdovers", "past lives", "the substance", "anora", "conclave",
          "wicked", "toy story", "top gun maverick", "mad max fury road", "everything everywhere all at once",
          "el hoyo", "ocho apellidos vascos", "the irishman", "no country for old men", "la la land",
          "spirited away", "the grand budapest hotel", "arrival", "the prestige", "whiplash"]
# Titles made of words that are also commands: they must still be played, not obeyed.
TRICKY_MOVIES = ["home alone", "up", "back to the future", "stop making sense", "inside out", "the rewind",
                 "run lola run", "fast x", "the volume", "next of kin", "pause", "mute"]
SERIES = ["the bear", "severance", "the last of us", "stranger things", "the office", "breaking bad",
          "better call saul", "shogun", "fallout", "arcane", "squid game", "house of the dragon", "game of thrones",
          "succession", "the boys", "the white lotus", "andor", "slow horses", "the penguin", "pluribus", "foundation",
          "community", "friends", "the sopranos", "the wire", "true detective", "ted lasso", "peaky blinders", "dark",
          "money heist", "la casa de papel", "the crown", "black mirror", "the expanse", "invincible", "bluey",
          "rick and morty", "the simpsons", "one piece", "attack on titan"]
YT = ["lofi hip hop", "cat videos", "how to make paella", "real madrid highlights", "mrbeast", "relaxing piano music",
      "nba highlights", "kurzgesagt", "linus tech tips", "the daily show", "f1 race highlights", "jazz for studying"]
TWITCH = ["ibai", "auronplay", "shroud", "xqc", "kai cenat", "ninja", "illojuan", "rubius", "thegrefg"]
APPS = {"tizentube": ["youtube", "you tube", "tizentube"], "stremio": ["stremio", "stream io"],
        "kodi": ["kodi", "codey"], "twitch": ["twitch"], "movistar": ["movistar", "movistar plus", "movistar tv"],
        "prime": ["prime video", "amazon prime", "prime"]}

rows = []


def add(text, expect, cat):
    rows.append({"text": text, "expect": expect, "cat": cat})


def num(n, words):
    return WORDS[n] if words and n < len(WORDS) else str(n)


def se_phrase(s, e):
    """A spoken season+episode, with the form chosen at random."""
    w = rng.random() < 0.55
    forms = [f"season {num(s, w)} episode {num(e, w)}", f"s{s}e{e}",
             f"s0{s}e0{e}" if s < 10 and e < 10 else f"s{s}e{e}",
             f"season {num(s, w)}, episode {num(e, w)}", f"episode {num(e, w)} of season {num(s, w)}",
             f"season {s} ep {e}", f"episode {num(e, w)} season {num(s, w)}"]
    if s < len(ORD) and e < len(ORD):
        forms += [f"the {ORD[e]} episode of season {num(s, w)}", f"{ORD[s]} season episode {num(e, w)}"]
    return rng.choice(forms)


def maybe_noise(text):
    """What speech-to-text hands over: sometimes capitalised with a full stop, sometimes a filler in front."""
    r = rng.random()
    if r < 0.15:
        text = rng.choice(["um ", "uh ", "hey tv ", "okay ", "can you ", "could you "]) + text
    elif r < 0.22:
        text = text + rng.choice([" please", " now"])
    if rng.random() < 0.4:
        text = text[0].upper() + text[1:] + "."
    return text


# ---- play a movie
PLAY = ["play {t}", "put on {t}", "watch {t}", "start {t}", "i want to watch {t}", "can you play {t}",
        "let's watch {t}",
        "i'd like to watch {t}", "put {t} on", "play the movie {t}", "i wanna watch {t}"]
for t in MOVIES:
    for tpl in rng.sample(PLAY, 3):
        exp = {"type": "play_title", "title": t}
        if "the movie" in tpl:
            exp["kind"] = "movie"
        add(maybe_noise(tpl.format(t=t)), exp, "play_movie")
for t in TRICKY_MOVIES:
    for tpl in ["play {t}", "put on the movie {t}", "watch {t}"]:
        exp = {"type": "play_title", "title": t}
        if "the movie" in tpl:
            exp["kind"] = "movie"
        add(tpl.format(t=t), exp, "play_tricky_title")

# ---- play a series, an episode or a season
SERIES_PLAY = ["play {t} {se}", "watch {t} {se}", "put on {t} {se}", "i want to watch {t} {se}",
               "can you play {t} {se}",
               "{t} {se}", "go to {t} {se}", "start {t} {se}"]
for t in SERIES:
    for _ in range(3):
        s, e = rng.randint(1, 6), rng.randint(1, 12)
        add(maybe_noise(rng.choice(SERIES_PLAY).format(t=t, se=se_phrase(s, e))),
            {"type": "play_title", "title": t, "season": s, "episode": e}, "play_episode")
    s = rng.randint(1, 5)
    tpl = rng.choice(["play {t} season {s}", "watch season {s} of {t}", "put on {t} season {s}"])
    add(maybe_noise(tpl.format(t=t, s=num(s, rng.random() < 0.5))), {"type": "play_title", "title": t, "season": s},
        "play_season")
    add(maybe_noise(rng.choice(["play {t}", "watch {t}", "put on {t}", "let's watch {t}"]).format(t=t)),
        {"type": "play_title", "title": t}, "play_series")
for t in rng.sample(SERIES, 10):
    tpl = rng.choice(["play {t} from the start", "watch {t} from the beginning", "play {t} from the beginning"])
    add(tpl.format(t=t),
        {"type": "play_title", "title": t, "restart": True}, "play_restart")
for t in rng.sample(SERIES, 6):
    add(rng.choice(["watch the series {t}", "play the show {t}", "put on the series {t}"]).format(t=t),
        {"type": "play_title", "title": t, "kind": "series"}, "play_kind")
for t in rng.sample(SERIES, 6):
    add(f"play the first episode of {t}", {"type": "play_title", "title": t, "episode": 1}, "play_episode")

# ---- an episode of the series that's on
for _ in range(16):
    s, e = rng.randint(1, 6), rng.randint(1, 12)
    w = rng.random() < 0.5
    tpl = rng.choice([f"go to season {num(s, w)} episode {num(e, w)}", f"season {num(s, w)} episode {num(e, w)}",
                      f"jump to episode {num(e, w)} of season {num(s, w)}", f"play season {s} episode {e} of this show",
                      f"switch to season {num(s, w)} episode {num(e, w)}"])
    add(maybe_noise(tpl), {"type": "play_title", "title": None, "season": s, "episode": e}, "episode_here")
for _ in range(8):
    e = rng.randint(2, 12)
    w = rng.random() < 0.5
    add(maybe_noise(rng.choice([f"episode {num(e, w)}", f"play episode {num(e, w)}", f"jump to episode {num(e, w)}",
                                f"go to episode {num(e, w)}"])),
        {"type": "play_title", "title": None, "episode": e}, "episode_here")
for s in (2, 3, 4):
    add(rng.choice([f"season {WORDS[s]}", f"go to season {s}", f"play season {WORDS[s]}"]),
        {"type": "play_title", "title": None, "season": s}, "episode_here")

# ---- continue
for t in rng.sample(SERIES + MOVIES, 18):
    add(maybe_noise(rng.choice(["continue {t}", "resume {t}", "keep watching {t}", "carry on with {t}",
                                "continue watching {t}", "finish {t}"]).format(t=t)),
        {"type": "continue_watching", "title": t}, "continue_title")
for text in ["continue where i left off", "keep watching what i was watching", "continue watching",
             "resume what i was watching", "continue the show", "carry on with the show", "resume where we left off",
             "keep watching", "continue playing the last episode"]:
    add(maybe_noise(text), {"type": "continue_watching", "title": None}, "continue_last")

# ---- next / previous episode
for text in ["next episode", "play the next episode", "skip this episode", "go to the next episode",
             "skip to the next episode", "next episode please", "play next episode", "the next episode", "next",
             "next one", "play the next one", "skip episode"]:
    add(maybe_noise(text), {"type": "next_episode", "offset": 1}, "next_episode")
for text in ["previous episode", "go back one episode", "play the previous episode", "go back an episode",
             "the previous episode", "play the previous one", "previous", "go to the previous episode",
             "last episode", "back one episode"]:
    add(maybe_noise(text), {"type": "next_episode", "offset": -1}, "previous_episode")

# ---- playback
for text in ["pause", "pause it", "pause the movie", "pause the show", "hold on", "wait", "pause please",
             "can you pause", "pause this", "wait a second", "hold on a second", "pause for a moment", "please please",
             "pause the episode"]:
    add(maybe_noise(text) if " " in text else text, {"type": "transport", "op": "pause"}, "pause")
for text in ["play", "resume", "unpause", "keep playing", "continue", "resume playback", "play it", "un pause",
             "start playing again", "go on", "carry on", "resume the movie", "play again"]:
    add(maybe_noise(text) if " " in text else text, {"type": "transport", "op": "resume"}, "resume")
for text in ["stop", "stop it", "stop playing", "stop the movie", "stop playback", "stop the episode", "stop the show"]:
    add(maybe_noise(text) if " " in text else text, {"type": "transport", "op": "stop"}, "stop")
for amount, spoken in [(30, "thirty seconds"), (120, "two minutes"), (300, "five minutes"), (10, "ten seconds"),
                       (90, "90 seconds"), (60, "a minute"), (15, "fifteen seconds"), (1200, "20 minutes"),
                       (45, "45 seconds")]:
    tpl = rng.choice(["skip forward {a}", "fast forward {a}", "skip ahead {a}", "forward {a}", "skip {a}",
                      "go forward {a}", "jump ahead {a}", "jump forward {a}"])
    add(maybe_noise(tpl.format(a=spoken)), {"type": "transport", "op": "forward", "seconds": amount}, "forward")
for text in ["fast forward", "skip ahead", "skip forward"]:
    add(text, {"type": "transport", "op": "forward"}, "forward")
for amount, spoken in [(10, "ten seconds"), (30, "30 seconds"), (60, "one minute"), (120, "two minutes"),
                       (15, "fifteen seconds"), (5, "five seconds"), (300, "5 minutes")]:
    tpl = rng.choice(["go back {a}", "rewind {a}", "back {a}", "jump back {a}", "skip back {a}", "rewind by {a}"])
    add(maybe_noise(tpl.format(a=spoken)), {"type": "transport", "op": "back", "seconds": amount}, "back")
for text in ["rewind", "go back a bit", "rewind a little"]:
    add(text, {"type": "transport", "op": "back"}, "back")
for secs, spoken in [(720, "twelve minutes"), (3900, "one hour five minutes"), (2700, "minute 45"),
                     (1800, "30 minutes"), (3600, "one hour"), (5400, "an hour and a half"), (600, "10 minutes"),
                     (4500, "1 hour 15 minutes"), (150, "two minutes thirty seconds")]:
    tpl = rng.choice(["go to {a}", "jump to {a}", "skip to {a}", "seek to {a}"])
    add(maybe_noise(tpl.format(a=spoken)), {"type": "transport", "op": "seek_to", "seconds": secs}, "seek_to")
for text in ["go to the middle", "jump to the middle", "skip to halfway", "go to the middle of this episode",
             "go to the halfway point"]:
    add(maybe_noise(text), {"type": "transport", "op": "seek_to", "percent": 50}, "seek_middle")
for text in ["start over", "from the beginning", "restart", "go back to the start", "start it again",
             "play it from the start", "go to the beginning", "back to the beginning", "start from the beginning",
             "rewind to the beginning", "start this over"]:
    add(maybe_noise(text) if " " in text else text, {"type": "transport", "op": "seek_to", "seconds": 0}, "restart")

# ---- volume
for text in ["volume up", "louder", "turn it up", "turn up the volume", "raise the volume", "increase the volume",
             "make it louder", "i can't hear", "it's too quiet", "turn the volume up", "more volume"]:
    add(maybe_noise(text) if " " in text else text, {"type": "volume", "op": "up"}, "volume_up")
for text in ["volume down", "quieter", "turn it down", "lower the volume", "decrease the volume", "too loud",
             "make it quieter", "it's too loud", "turn the volume down", "less volume"]:
    add(maybe_noise(text) if " " in text else text, {"type": "volume", "op": "down"}, "volume_down")
for amt, text in [(3, "a bit louder"), (15, "turn it up a lot"), (3, "turn it down a bit"), (5, "volume up by five"),
                  (10, "turn the volume down ten"), (5, "volume down 5")]:
    op = "up" if any(w in text for w in ("up", "louder")) else "down"
    add(text, {"type": "volume", "op": op, "amount": amt}, "volume_amount")
TENS_SPOKEN = {25: "twenty five", 30: "thirty", 35: "thirty five", 40: "forty", 50: "fifty", 60: "sixty"}
for lvl in (20, 35, 10, 25, 40, 15, 50, 60, 5, 30):
    w = rng.random() < 0.5
    tpl = rng.choice(["volume {n}", "set the volume to {n}", "volume at {n}", "put the volume at {n}",
                      "change the volume to {n}", "volume to {n}", "set volume {n}", "turn the volume to {n}"])
    spoken = num(lvl, w) if lvl <= 24 else (TENS_SPOKEN[lvl] if w else str(lvl))
    add(maybe_noise(tpl.format(n=spoken)), {"type": "volume", "op": "set", "amount": lvl}, "volume_set")
for text in ["mute", "mute it", "mute the tv", "silence", "mute the sound"]:
    add(text, {"type": "volume", "op": "mute"}, "mute")
for text in ["unmute", "sound on", "turn the sound back on", "unmute the tv"]:
    add(text, {"type": "volume", "op": "unmute"}, "unmute")

# ---- apps, YouTube, Twitch, Home, Stremio pages
for key, aliases in APPS.items():
    for alias in aliases:
        tpl = rng.choice(["open {a}", "launch {a}", "go to {a}", "start {a}", "switch to {a}", "take me to {a}",
                          "open the {a} app"])
        add(maybe_noise(tpl.format(a=alias)), {"type": "open_app", "app": key}, "open_app")
for text, key in [("kodi", "kodi"), ("stremio", "stremio"), ("prime video", "prime"), ("twitch", "twitch"),
                  ("Open Stream U", "stremio"), ("Open Mobistar Plus", "movistar"),
                  ("go back to youtube", "tizentube")]:
    add(text, {"type": "open_app", "app": key}, "open_app")
for q in YT:
    tpl = rng.choice(["play {q} on youtube", "search youtube for {q}", "youtube {q}", "{q} on youtube",
                      "put on {q} on youtube", "find {q} on youtube"])
    add(maybe_noise(tpl.format(q=q)), {"type": "youtube", "query": q}, "youtube")
for ch in TWITCH:
    tpl = rng.choice(["put {c} on twitch", "watch {c} on twitch", "twitch {c}", "open {c} on twitch"])
    add(maybe_noise(tpl.format(c=ch)), {"type": "twitch", "channel": ch}, "twitch")
for text in ["go home", "home", "home screen", "go to the home screen", "take me home", "go to home",
             "back to the home screen"]:
    add(maybe_noise(text) if " " in text else text, {"type": "home"}, "home")
for t in rng.sample(SERIES + MOVIES, 10):
    tpl = rng.choice(["open {t} in stremio", "show me {t} in stremio", "{t} in stremio", "find {t} on stremio"])
    add(maybe_noise(tpl.format(t=t)), {"type": "open_in_stremio", "title": t}, "stremio_page")

# ---- not commands: nothing should happen
for text in ["what's the weather like", "what time is it", "thank you", "tell me a joke", "order a pizza",
             "who's that actor", "i love this show", "turn on the lights", "call mom", "set an alarm for seven",
             "how old is pedro pascal", "the", "uh", "and then", "hmm", "yes", "no", "okay", "",
             "i told you we should have left earlier", "where are my keys", "what's for dinner",
             "play a game", "good night", "is it raining", "send a message to juan"]:
    add(text, {"type": "none"}, "not_a_command")

# ---- binge mode, sleep timer, subtitles and audio, "what did they say?"
for text, on in [("binge mode on", True), ("turn on binge mode", True), ("turn off autoplay", False),
                 ("autoplay off", False), ("enable autoplay", True), ("disable binge mode", False),
                 ("stop autoplay", False), ("binge mode off", False)]:
    add(maybe_noise(text), {"type": "binge", "on": on}, "binge")
for text, then in [("stop after this episode", "stop"), ("stop after this one", "stop"),
                   ("this is the last episode", "stop"), ("turn off the tv after this episode", "off"),
                   ("turn off after this one", "off"), ("go to sleep after this episode", "off")]:
    add(maybe_noise(text), {"type": "timer", "then": then, "minutes": None}, "timer_after")
for text, mins, then in [("turn off in 30 minutes", 30, "off"), ("turn off the tv in half an hour", 30, "off"),
                         ("sleep timer 45 minutes", 45, "off"), ("set a sleep timer for an hour", 60, "off"),
                         ("stop in 20 minutes", 20, "stop"), ("stop playing in ten minutes", 10, "stop")]:
    add(maybe_noise(text), {"type": "timer", "then": then, "minutes": mins}, "timer_in")
for text in ["cancel the sleep timer", "cancel the timer", "keep playing after this"]:
    add(text, {"type": "timer", "cancel": True}, "timer_cancel")
for text, subs in [("subtitles off", "off"), ("turn off the subtitles", "off"), ("no subtitles", "off"),
                   ("turn on subtitles", "on"), ("subtitles on", "on"), ("spanish subtitles", "spa"),
                   ("put on english subtitles", "eng"), ("subtitles in spanish", "spa"),
                   ("traditional chinese subtitles", "cht"), ("switch to simplified chinese subtitles", "chs")]:
    add(maybe_noise(text), {"type": "tracks", "subtitles": subs}, "subtitles")
for text, audio in [("original audio", "original"), ("switch to the original language", "original"),
                    ("english audio", "eng"), ("switch to spanish audio", "spa"), ("play it in spanish", "spa"),
                    ("put it in english", "eng")]:
    add(maybe_noise(text), {"type": "tracks", "audio": audio}, "audio")
for text in ["what did they say", "what did he just say", "say that again", "i missed that", "repeat that"]:
    add(maybe_noise(text), {"type": "replay", "seconds": 10}, "replay")

# ---- lists on the TV, "number 2", the latest episode, your library
for text in ["what was i watching", "what were we watching", "what have i been watching lately",
             "show me what i was watching", "show my continue watching", "open continue watching",
             "what did we watch last", "recently watched"]:
    add(maybe_noise(text), {"type": "show_list", "list": "recent"}, "list_recent")
for text in ["what's new", "anything new", "is there anything new on my shows", "any new episodes", "new episodes",
             "show me new episodes", "what's new this week", "are there any new episodes of my shows"]:
    add(maybe_noise(text), {"type": "show_list", "list": "new"}, "list_new")
for text in ["new movies", "show me new films", "what new movies are there", "latest movies", "any new films"]:
    add(maybe_noise(text), {"type": "show_list", "list": "movies"}, "list_movies")
for n in range(1, 9):
    forms = [f"number {num(n, True)}", f"number {n}", f"play number {num(n, True)}", f"option {num(n, True)}",
             f"the {ORD[n]} one", f"pick the {ORD[n]} one", num(n, True)]
    for form in rng.sample(forms, 2):
        add(maybe_noise(form), {"type": "pick", "number": n}, "pick")
for title in rng.sample(SERIES, 8):
    form = rng.choice([f"play the latest episode of {title}", f"latest episode of {title}",
                       f"the newest episode of {title}", f"put on the new episode of {title}",
                       f"watch the most recent episode of {title}"])
    add(maybe_noise(form), {"type": "play_title", "title": title, "latest": True}, "latest_episode")
for text, expect in [("add this to my library", {"op": "add", "title": None}), ("save this", {"op": "add", "title": None}),
                     ("add it to my list", {"op": "add", "title": None}),
                     ("add the bear to my library", {"op": "add", "title": "the bear"}),
                     ("put dune part two in my watchlist", {"op": "add", "title": "dune part two"}),
                     ("remove friends from my library", {"op": "remove", "title": "friends"}),
                     ("take this off my list", {"op": "remove", "title": None}),
                     ("mark this as watched", {"op": "watched", "title": None}),
                     ("mark as watched", {"op": "watched", "title": None}),
                     ("i've already seen this", {"op": "watched", "title": None}),
                     ("mark the bear season 3 as watched", {"op": "watched", "title": "the bear", "season": 3}),
                     ("mark severance season two episode four as watched",
                      {"op": "watched", "title": "severance", "season": 2, "episode": 4}),
                     ("mark it as unwatched", {"op": "unwatched", "title": None}),
                     ("mark the office as not watched", {"op": "unwatched", "title": "the office"})]:
    add(maybe_noise(text), {"type": "library", **expect}, "library")
for text in ["no one", "one more thing", "two people walked in", "number one fan"]:
    add(text, {"type": "none"}, "not_a_command")

for r in rows:
    r["split"] = "test" if int(hashlib.sha1(r["text"].encode()).hexdigest(), 16) % 10 < 3 else "dev"
seen, unique = set(), []
for r in rows:
    if r["text"].lower() not in seen:
        seen.add(r["text"].lower())
        unique.append(r)
OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in unique))
print(f"{len(unique)} commands ({sum(r['split'] == 'test' for r in unique)} held out) -> {OUT}")
