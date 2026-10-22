"""Everything you can say, by topic, with English and Spanish examples (Spanish needs the multilingual speech model).
Shown by the panel's Commands tab and by the TV's "what can you do?" screen. Every example is checked against the
rules by a test (tests/test_core.py), so this list can't drift from what works. Stdlib only: the panel imports it."""

COMMANDS = [
    ("Films and series", [
        (["play The Bear season 3 episode 2", "put on Dune Part Two"],
         ["pon The Bear temporada tres capítulo dos", "quiero ver el hoyo"],
         "Finds it (by its name in English or Spanish) and plays it in Kodi from your Stremio addons, where you "
         "stopped"),
        (["play Severance from the start"], ["pon Severance desde el principio"], "From the beginning instead"),
        (["play the latest episode of The Bear"], ["pon el último capítulo de The Bear"], "The newest episode out"),
        (["continue The Office", "keep watching"], ["sigue viendo The Office"],
         "Where you left off (the next episode if you'd finished one)"),
        (["next episode", "previous episode"], ["siguiente capítulo", "capítulo anterior"],
         "Of the series that's on"),
        (["go to season 4 episode 2", "episode five"], ["ve a la temporada cuatro capítulo dos"],
         "Another episode of the series that's on"),
        (["show me The Office in Stremio"], [], "Opens its page in Stremio without playing"),
    ]),
    ("Lists on the TV", [
        (["what was I watching?"], ["¿qué estaba viendo?"], "Your Continue Watching as a numbered list"),
        (["what's new?"], ["¿qué hay de nuevo?"], "The newest episode of each of your shows you haven't got to"),
        (["new films"], ["películas nuevas"], "Your addons' list of new films"),
        (["number 2", "the third one"], ["número dos", "el tercero"],
         "Plays that one from the list (or OK / a number key on the remote)"),
        (["what can you do?"], ["¿qué puedes hacer?"], "This list, on the TV"),
    ]),
    ("Playback", [
        (["pause", "resume", "stop"], ["pausa", "sigue", "para"], "Stop goes back to Home"),
        (["skip forward thirty seconds", "go back ten seconds"], ["adelanta treinta segundos", "atrasa diez segundos"],
         "Exact in Kodi"),
        (["go to twelve minutes", "go to the middle", "start over"],
         ["ve al minuto doce", "ve a la mitad", "desde el principio"], "Jumps to that point"),
        (["what did they say?"], ["¿qué ha dicho?"], "Back 10 s, with subtitles on for a moment"),
    ]),
    ("Intros, recaps and credits", [
        (["skip the intro", "skip the recap"], ["salta la intro", "salta el resumen"],
         "To its end, when you're in it (timestamps from TheIntroDB and IntroDB)"),
        (["skip the credits"], ["salta los créditos"], "The next episode in binge mode, else Home"),
        (["auto skip on", "auto skip off"], [], "Intros and recaps skipped by themselves when the timestamps are sure"),
    ]),
    ("Subtitles and audio", [
        (["subtitles on", "subtitles off", "Spanish subtitles"],
         ["pon subtítulos", "quita los subtítulos", "subtítulos en inglés"],
         "The file's own, else your subtitle addons"),
        (["original audio", "audio in English"], ["versión original", "ponlo en inglés"], "Switches the audio track"),
    ]),
    ("Volume", [
        (["volume twenty", "louder", "turn it down a bit"], ["volumen al veinte", "sube el volumen", "baja un poco"],
         "TV volume 0-100"),
        (["mute", "unmute"], ["silencio", "pon el sonido"], ""),
    ]),
    ("Binge mode and sleep timer", [
        (["binge mode on", "binge mode off"], ["activa el modo maratón", "desactiva el modo maratón"],
         "When an episode ends, the next one starts"),
        (["stop after this episode", "turn the TV off after this one"],
         ["para después de este capítulo", "apaga la tele después de este capítulo"], "Wins over binge mode"),
        (["stop in 30 minutes", "turn off in an hour"], ["para en treinta minutos", "apaga la tele en una hora"], ""),
        (["cancel the sleep timer"], ["cancela el temporizador"], ""),
    ]),
    ("Apps", [
        (["open YouTube", "open Movistar", "go home"], ["abre YouTube", "abre Movistar", "inicio"], ""),
        (["lofi hip hop on YouTube", "search YouTube for paella"], ["busca recetas de paella en YouTube"],
         "The top result in TizenTube"),
        (["Ibai on Twitch"], ["Ibai en Twitch"], "The live stream"),
    ]),
    ("Your Stremio library", [
        (["add this to my library", "remove Friends from my library"], ["añádelo a mi biblioteca"], ""),
        (["mark this as watched", "mark The Bear season 3 as watched", "mark it as unwatched"],
         ["márcalo como visto", "márcalo como no visto"], "Stremio's own watched marks"),
    ]),
    ("When it gets it wrong", [
        (["that's wrong"], ["eso está mal"],
         "Marks the last command; teach it the right action in the panel (Activity, right-click)"),
    ]),
]
