"""Which audio and subtitle tracks to use for a title, and how to find them among a file's tracks.

Your rule (2026-10-22): a title made in a Spanish-speaking country plays in Spanish with no subtitles;
anything else plays in English with subtitles on (English, else Spanish). Settings since 2026-10-24: the audio
(`audio_language`: a language, or "original", the one the title was made in) and when subtitles show
(`subtitles`: "foreign", only while the audio isn't your native_language; "always"; "off"). The language a title was
made in comes from the files' own tracks when your addons say which one is the original (AIOStreams reads them),
else from the first country Cinemeta lists: it has no language field.
"""
import re
from dataclasses import dataclass

SPANISH_COUNTRIES = {
    "spain", "mexico", "argentina", "colombia", "chile", "peru", "venezuela", "uruguay", "cuba", "ecuador",
    "bolivia", "paraguay", "dominican republic", "guatemala", "costa rica", "panama", "honduras", "el salvador",
    "nicaragua", "puerto rico",
}
# The language of a title made there, when no file says (the first country Cinemeta lists)
COUNTRY_LANGUAGE = {**{c: "spa" for c in SPANISH_COUNTRIES},
                    **{c: "eng" for c in ("united states", "usa", "united kingdom", "uk", "canada", "australia",
                                          "ireland", "new zealand")},
                    "france": "fre", "italy": "ita", "germany": "ger", "austria": "ger", "portugal": "por",
                    "brazil": "por", "japan": "jpn", "south korea": "kor", "korea": "kor", "china": "chi",
                    "taiwan": "chi", "hong kong": "chi", "russia": "rus", "india": "hin", "turkey": "tur",
                    "poland": "pol", "netherlands": "dut", "sweden": "swe", "denmark": "dan", "norway": "nor",
                    "finland": "fin"}
# Kodi reports ISO 639-2 codes, sometimes 639-1 or a name, depending on the file.
CHINESE = {"chi", "zho", "zh", "chinese", "中文", "chs", "cht", "zh-hans", "zh-hant", "zh-cn", "zh-tw", "zh-hk",
           "cmn", "yue", "mandarin", "cantonese"}
ALIASES = {"eng": {"eng", "en", "english"}, "spa": {"spa", "es", "spanish", "español", "castellano", "esp"},
           "fre": {"fre", "fra", "fr", "french"}, "ita": {"ita", "it", "italian"}, "ger": {"ger", "deu", "de", "german"},
           "por": {"por", "pt", "portuguese", "portuguese (brazil)"}, "jpn": {"jpn", "ja", "japanese"},
           "kor": {"kor", "ko", "korean"}, "rus": {"rus", "ru", "russian"}, "hin": {"hin", "hi", "hindi"},
           "tur": {"tur", "tr", "turkish"}, "pol": {"pol", "pl", "polish"}, "dut": {"dut", "nld", "nl", "dutch"},
           "swe": {"swe", "sv", "swedish"}, "dan": {"dan", "da", "danish"}, "nor": {"nor", "nob", "no", "norwegian"},
           "fin": {"fin", "fi", "finnish"}, "chi": CHINESE, "chs": CHINESE, "cht": CHINESE}
# A language's code from any of its names or codes ("Spanish", "es", "spa" -> "spa"; "Latino" counts as Spanish)
CODES = {**{n: code for code, names in ALIASES.items() if code not in ("chs", "cht") for n in names},
         "latino": "spa", "latin american": "spa"}
# Simplified vs Traditional Chinese: the track's language says "Chinese"; its name usually says which.
SIMPLIFIED = re.compile(r"simplified|简体|簡體|简|\bchs\b|hans|\bsc\b|zh-?cn|chinese \(prc\)|mandarin", re.I)
TRADITIONAL = re.compile(r"traditional|繁體|繁体|繁|\bcht\b|hant|\btc\b|zh-?tw|zh-?hk|taiwan|hong ?kong|cantonese|big5", re.I)
# Kodi's names in its language settings (locale.audiolanguage / locale.subtitlelanguage).
KODI_NAMES = {"eng": "English", "spa": "Spanish", "fre": "French", "ita": "Italian", "ger": "German",
              "por": "Portuguese", "jpn": "Japanese", "kor": "Korean", "chi": "Chinese", "chs": "Chinese",
              "cht": "Chinese", "rus": "Russian", "hin": "Hindi", "tur": "Turkish", "pol": "Polish", "dut": "Dutch",
              "swe": "Swedish", "dan": "Danish", "nor": "Norwegian", "fin": "Finnish"}
# Commentary and audio-description tracks: never the one to play by default.
EXTRA_AUDIO = re.compile(r"comment|коментар|комментар|director|descri|\bAD\b", re.I)
# Android caption locales for Just Player (it only tells languages apart, not the two Chinese scripts).
ANDROID_LOCALE = {"eng": "en", "spa": "es", "fre": "fr", "ita": "it", "ger": "de", "por": "pt", "jpn": "ja",
                  "kor": "ko", "chi": "zh", "chs": "zh_CN", "cht": "zh_TW"}


@dataclass(frozen=True)
class TrackPlan:
    audio: str | None              # language to switch audio to, if the file has it; None: the file's default
    subtitles: list[str] | None    # languages to try in order; None = subtitles off
    unless: str | None = None      # no subtitles while the audio playing is in this language (yours)
    original: bool = False         # the audio wanted is the title's original, its language not known: the file's
                                   # track marked original, else its default

    def subs_for(self, audio: str | None) -> list[str] | None:
        """The subtitles to show with audio in this language (None: not known)."""
        if self.subtitles is None or (self.unless and audio and code(audio) == code(self.unless)):
            return None
        return self.subtitles


def original_country(meta: dict) -> str:
    return str(meta.get("country") or "").split(",")[0].strip().lower()


def code(name: str | None) -> str | None:
    """A language's code from a name or code ("Spanish", "es", "spa" -> "spa"), else None."""
    name = (name or "").strip().lower()
    return CODES.get(name) or (name if name in ALIASES else None)


def made_in(meta: dict, original: str | None = None) -> str | None:
    """The language the title was made in: the files' original audio when known, else by its first country."""
    return original or COUNTRY_LANGUAGE.get(original_country(meta))


def plan(meta: dict, audio_language: str, subtitle_languages: list[str], native_language: str,
         subtitles: str = "foreign", original: str | None = None, asked: str | None = None) -> TrackPlan:
    """A title made in your language plays in it; anything else in audio_language ("original": the language it was
    made in, when known). `asked`: the audio asked for this title by voice, which goes first. Subtitles in your
    order, shown per `subtitles` (config.SUBTITLES_WHEN)."""
    first = made_in(meta, original)
    if asked:
        audio = first if asked == "original" else asked
    elif first == native_language:
        audio = native_language
    else:
        audio = first if audio_language == "original" else audio_language
    return TrackPlan(audio, None if subtitles == "off" else list(subtitle_languages),
                     native_language if subtitles == "foreign" else None,
                     original=audio is None and (asked == "original" or not asked and audio_language == "original"))


def matches(track: dict, lang: str) -> bool:
    code = str(track.get("language") or "").lower()
    if code not in ALIASES.get(lang, {lang}):
        return False
    if lang in ("chs", "cht"):
        hint = f"{track.get('name') or ''} {code}"
        simplified, traditional = bool(SIMPLIFIED.search(hint)), bool(TRADITIONAL.search(hint))
        if lang == "chs":
            return simplified or (code in ("chs", "zh-hans", "zh-cn"))
        return traditional or (code in ("cht", "zh-hant", "zh-tw", "zh-hk"))
    return True


def name(code: str | None) -> str:
    """'spa' -> 'Spanish' (Kodi's names); unknown codes as they are."""
    code = (code or "").lower()
    found = next((key for key, names in ALIASES.items() if code in names), code)
    return KODI_NAMES.get(found, code)


def android_locale(lang: str) -> str:
    return ANDROID_LOCALE.get(lang, lang[:2])


def pick_audio(tracks: list[dict], lang: str) -> dict | None:
    """The track in that language, not a commentary or audio description; for Spanish, Castilian before Latin
    American."""
    found = [t for t in tracks if matches(t, lang)]
    found.sort(key=lambda t: (bool(t.get("isimpaired")) or bool(EXTRA_AUDIO.search(str(t.get("name") or ""))),
                              lang == "spa" and any(w in str(t.get("name") or "").lower()
                                                    for w in ("latino", "latin", "lat "))))
    return found[0] if found else None


def pick_subtitle(tracks: list[dict], langs: list[str]) -> dict | None:
    """First language that has a full (not forced) track; plain before SDH; Castilian before Latin American."""
    for lang in langs:
        found = [t for t in tracks if matches(t, lang) and not t.get("isforced")]
        found.sort(key=lambda t: ("sdh" in str(t.get("name") or "").lower() or bool(t.get("isimpaired")),
                                  lang == "spa" and any(w in str(t.get("name") or "").lower()
                                                        for w in ("latino", "latin", "lat "))))
        if found:
            return found[0]
    return None
