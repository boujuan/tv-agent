"""Everything you can say, by section, as patterns: ‹placeholders› for what you fill in, [optional words], “/” between
single-word alternatives (“on/off”) and “ · ” between whole ones. In English and Spanish (Spanish needs the
multilingual speech model). Shown by the panel's Commands tab and by the TV's "what can you do?" screen, in the
language it was asked in.

A test (tests/test_core.py) expands every pattern into the sentences it stands for, with the placeholders filled
from EXAMPLES, and checks the rules give each one the action listed here: this list can't drift from what works.
Other phrasings often work too (the LLM and d1 steps). Stdlib only: the panel imports it.
"""
import re
from typing import NamedTuple


class Command(NamedTuple):
    en: str
    es: str
    does_en: str
    does_es: str
    type: str                 # the action every sentence of the pattern gives

    def say(self, lang: str) -> str:
        return self.es if lang == "es" else self.en

    def does(self, lang: str) -> str:
        return self.does_es if lang == "es" else self.does_en


class Section(NamedTuple):
    en: str
    es: str
    commands: list[Command]

    def name(self, lang: str) -> str:
        return self.es if lang == "es" else self.en


SECTIONS = [
    Section("Films and series", "Películas y series", [
        Command("play ‹title›", "pon ‹título›",
                "Finds it by its name, in English or Spanish, and plays it where you stopped",
                "Lo busca por su nombre, en español o en inglés, y lo pone donde lo dejaste", "play_title"),
        Command("play ‹title› season ‹n› [episode ‹n›]", "pon ‹título› temporada ‹n› [capítulo ‹n›]",
                "That season or episode", "Esa temporada o capítulo", "play_title"),
        Command("play ‹title› from the start", "pon ‹título› desde el principio",
                "From the beginning", "Desde el principio", "play_title"),
        Command("play the latest episode of ‹title›", "pon el último capítulo de ‹título›",
                "The newest episode out", "El último capítulo que ha salido", "play_title"),
        Command("continue ‹title›", "sigue viendo ‹título›",
                "Where you left off, or the next episode", "Donde lo dejaste, o el capítulo siguiente",
                "continue_watching"),
        Command("next/previous episode", "siguiente capítulo · capítulo anterior",
                "Of the series that's on", "De la serie que está puesta", "next_episode"),
        Command("go to season ‹n› episode ‹n› · episode ‹n›", "ve a la temporada ‹n› capítulo ‹n› · capítulo ‹n›",
                "Another episode of the series that's on", "Otro capítulo de la serie que está puesta", "play_title"),
        Command("show ‹title› in Stremio", "enséñame ‹título› en Stremio",
                "Its page in Stremio, without playing", "Su página en Stremio, sin ponerlo", "open_in_stremio"),
    ]),
    Section("What's on", "Qué estás viendo", [
        Command("what am I watching?", "¿qué estoy viendo?",
                "The title, how far in and when it ends", "El título, por dónde va y cuándo acaba", "info"),
        Command("where am I in ‹title›?", "¿por dónde voy de ‹título›?",
                "Your last episode of it and how far in", "Tu último capítulo y por dónde ibas", "info"),
    ]),
    Section("Lists on the TV", "Listas en la tele", [
        Command("what was I watching?", "¿qué estaba viendo?",
                "Your Continue Watching, numbered", "Tu «Seguir viendo», numerado", "show_list"),
        Command("what's new?", "¿qué hay de nuevo?",
                "New episodes of your shows", "Capítulos nuevos de tus series", "show_list"),
        Command("new films", "películas nuevas",
                "Your addons' list of new films", "La lista de películas nuevas de tus addons", "show_list"),
        Command("number ‹n› · the ‹first, second…› one", "número ‹n› · el ‹primero, segundo…›",
                "Plays that one from the list (or OK, or a number key)",
                "Pone ese de la lista (o OK, o una tecla numérica)", "pick"),
        Command("what can you do?", "¿qué puedes hacer?", "This list", "Esta lista", "help"),
    ]),
    Section("Playback", "Reproducción", [
        Command("pause · resume · stop", "pausa · sigue · para",
                "Stop goes back to Home", "Parar vuelve al inicio", "transport"),
        Command("skip forward ‹time› · go back ‹time›", "adelanta ‹tiempo› · atrasa ‹tiempo›",
                "Exact in Kodi", "Exacto en Kodi", "transport"),
        Command("go to ‹time›", "ve al minuto ‹n›", "Jumps to that point", "Salta a ese punto", "transport"),
        Command("go to the middle · start over", "ve a la mitad · desde el principio",
                "Half way, or the beginning", "La mitad, o el principio", "transport"),
        Command("what did they say?", "¿qué ha dicho?",
                "Back 10 s, with subtitles for a moment", "Atrás 10 s, con subtítulos un momento", "replay"),
    ]),
    Section("Intros, recaps and credits", "Intros, resúmenes y créditos", [
        Command("skip the intro/recap", "salta la intro · salta el resumen",
                "To its end, when you're in it", "Hasta su final, si estás en ella", "skip"),
        Command("skip the credits", "salta los créditos",
                "The next episode in binge mode, else Home", "El siguiente capítulo en modo maratón; si no, al inicio",
                "skip"),
        Command("auto skip on/off", "activa/desactiva el salto automático",
                "Intros and recaps skipped by themselves", "Intros y resúmenes saltados solos", "auto_skip"),
    ]),
    Section("Subtitles and audio", "Subtítulos y audio", [
        Command("subtitles on/off · subtitles", "pon subtítulos · quita los subtítulos · subtítulos",
                "On, off, or just “subtitles” to switch them over", "Ponerlos, quitarlos, o solo «subtítulos» para "
                "cambiar", "tracks"),
        Command("‹language› subtitles", "subtítulos en ‹idioma›",
                "From the file, else your subtitle addons", "Del archivo, o de tus addons de subtítulos", "tracks"),
        Command("audio in ‹language› · original audio", "ponlo en ‹idioma› · versión original",
                "Switches the audio track", "Cambia la pista de audio", "tracks"),
    ]),
    Section("Volume", "Volumen", [
        Command("volume ‹level›", "volumen al ‹nivel›",
                "0 to 100, or “the max”, “the minimum”", "De 0 a 100, o «al máximo», «al mínimo»", "volume"),
        Command("louder/quieter [a bit · a lot]", "sube/baja [un poco · mucho]", "", "", "volume"),
        Command("mute/unmute", "silencio · pon el sonido", "", "", "volume"),
    ]),
    Section("Binge mode and sleep timer", "Modo maratón y temporizador", [
        Command("binge mode on/off", "activa/desactiva el modo maratón",
                "When an episode ends, the next one starts", "Al acabar un capítulo empieza el siguiente", "binge"),
        Command("stop after this episode · turn the TV off after this one",
                "para después de este capítulo · apaga la tele después de este capítulo",
                "Wins over binge mode", "Gana al modo maratón", "timer"),
        Command("stop in ‹time› · turn off in ‹time›", "para en ‹tiempo› · apaga la tele en ‹tiempo›", "", "", "timer"),
        Command("cancel the sleep timer", "cancela el temporizador", "", "", "timer"),
    ]),
    Section("Apps", "Apps", [
        Command("open ‹app›", "abre ‹app›", "An app on the list (Decisions › Apps)",
                "Una app de la lista (Decisiones › Apps)", "open_app"),
        Command("go home", "inicio", "The TV's home screen", "La pantalla de inicio", "home"),
        Command("‹search› on YouTube", "busca ‹búsqueda› en YouTube",
                "The top result, in TizenTube", "El primer resultado, en TizenTube", "youtube"),
        Command("‹channel› on Twitch", "‹canal› en Twitch", "The live stream", "El directo", "twitch"),
    ]),
    Section("Your Stremio library", "Tu biblioteca de Stremio", [
        Command("add ‹title› to my library · remove ‹title› from my library",
                "añade ‹título› a mi biblioteca · quita ‹título› de mi biblioteca",
                "“this” for what's on", "«añádelo» para lo que está puesto", "library"),
        Command("mark ‹title› as watched/unwatched", "marca ‹título› como [no] visto",
                "Stremio's own watched marks", "Las marcas de visto de Stremio", "library"),
    ]),
    Section("Two at once", "Dos a la vez", [
        Command("‹command› and ‹command›", "‹orden› y ‹orden›",
                "One after the other: “play The Bear and skip the intro”",
                "Una tras otra: «pon The Bear y salta la intro»", "sequence"),
    ]),
    Section("When it gets it wrong", "Si se equivoca", [
        Command("that's wrong", "eso está mal",
                "Marks the last command: teach it in the panel (Activity)",
                "Marca la última orden: enséñale la buena en el panel (Actividad)", "wrong"),
    ]),
]

LEGEND = {"en": "‹…› what you fill in · [ … ] optional · a/b one or the other · “ · ” between alternatives",
          "es": "‹…› lo que pones tú · [ … ] opcional · a/b uno u otro · “ · ” entre alternativas"}
TV_HINT = {"en": "Press the mic and say one · ▲▼ to scroll · BACK closes",
           "es": "Pulsa el micro y di una · ▲▼ para moverte · ATRÁS cierra"}

# What the placeholders stand for in the examples (the tests, the panel's "Try it"); a list is used in order
EXAMPLES = {
    "en": {"title": "The Bear", "n": "three", "time": "twenty minutes", "language": "Spanish", "app": "YouTube",
           "search": "paella recipes", "channel": "Ibai", "level": "twenty", "first, second…": "third",
           "command": ["play The Bear season three", "skip the intro"]},
    "es": {"título": "La casa de papel", "n": "tres", "tiempo": "veinte minutos", "idioma": "inglés", "app": "YouTube",
           "búsqueda": "recetas de paella", "canal": "Ibai", "nivel": "veinte", "primero, segundo…": "tercero",
           "orden": ["pon La casa de papel", "salta la intro"]},
}
PLACEHOLDER = re.compile(r"‹([^›]+)›")


def _split(text: str, sep: str = " · ") -> list[str]:
    """Split on sep outside [brackets]."""
    out, depth, cur, i = [], 0, "", 0
    while i < len(text):
        if text.startswith(sep, i) and depth == 0:
            out.append(cur)
            cur, i = "", i + len(sep)
            continue
        depth += {"[": 1, "]": -1}.get(text[i], 0)
        cur += text[i]
        i += 1
    return out + [cur]


def _optional(text: str) -> list[str]:
    m = re.search(r"\[([^\[\]]*)\]", text)
    if not m:
        return [text]
    out = []
    for inner in [""] + _split(m.group(1)):
        out += _optional(text[:m.start()] + inner + text[m.end():])
    return out


def _slashes(text: str) -> list[str]:
    m = re.search(r"(\S+)/(\S+)", text)
    if not m:
        return [text]
    return _slashes(text[:m.start()] + m.group(1) + text[m.end():]) + \
        _slashes(text[:m.start()] + m.group(2) + text[m.end():])


def expand(pattern: str, lang: str, examples: dict | None = None) -> list[str]:
    """Every sentence a pattern stands for, with its placeholders filled in."""
    fills = examples or EXAMPLES[lang]
    out = []
    for alt in _split(pattern):
        for sentence in (s for o in _optional(alt) for s in _slashes(o)):
            used: dict[str, int] = {}

            def fill(m):
                value = fills.get(m.group(1), m.group(1))
                if isinstance(value, list):
                    used[m.group(1)] = used.get(m.group(1), -1) + 1
                    value = value[min(used[m.group(1)], len(value) - 1)]
                return value
            out.append(re.sub(r"\s+", " ", PLACEHOLDER.sub(fill, sentence)).strip())
    return out
