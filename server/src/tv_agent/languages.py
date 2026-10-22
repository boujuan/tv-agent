"""Which audio and subtitle tracks to use for a title, and how to find them among a file's tracks.

Your rule (2026-10-22): a title made in a Spanish-speaking country plays in Spanish with no subtitles;
anything else plays in English with subtitles on (English, else Spanish). Cinemeta has no language field, so
"made in" is the first country it lists.
"""
import re
from dataclasses import dataclass

SPANISH_COUNTRIES = {
    "spain", "mexico", "argentina", "colombia", "chile", "peru", "venezuela", "uruguay", "cuba", "ecuador",
    "bolivia", "paraguay", "dominican republic", "guatemala", "costa rica", "panama", "honduras", "el salvador",
    "nicaragua", "puerto rico",
}
NATIVE_COUNTRIES = {"spa": SPANISH_COUNTRIES}
# Kodi reports ISO 639-2 codes, sometimes 639-1 or a name, depending on the file.
CHINESE = {"chi", "zho", "zh", "chinese", "中文", "chs", "cht", "zh-hans", "zh-hant", "zh-cn", "zh-tw", "zh-hk",
           "cmn", "yue", "mandarin", "cantonese"}
ALIASES = {"eng": {"eng", "en", "english"}, "spa": {"spa", "es", "spanish", "español", "castellano", "esp"},
           "fre": {"fre", "fra", "fr", "french"}, "ita": {"ita", "it", "italian"}, "ger": {"ger", "deu", "de", "german"},
           "por": {"por", "pt", "portuguese"}, "jpn": {"jpn", "ja", "japanese"}, "kor": {"kor", "ko", "korean"},
           "chi": CHINESE, "chs": CHINESE, "cht": CHINESE}
# Simplified vs Traditional Chinese: the track's language says "Chinese"; its name usually says which.
SIMPLIFIED = re.compile(r"simplified|简体|簡體|简|\bchs\b|hans|\bsc\b|zh-?cn|chinese \(prc\)|mandarin", re.I)
TRADITIONAL = re.compile(r"traditional|繁體|繁体|繁|\bcht\b|hant|\btc\b|zh-?tw|zh-?hk|taiwan|hong ?kong|cantonese|big5", re.I)
# Kodi's names in its language settings (locale.audiolanguage / locale.subtitlelanguage).
KODI_NAMES = {"eng": "English", "spa": "Spanish", "fre": "French", "ita": "Italian", "ger": "German",
              "por": "Portuguese", "jpn": "Japanese", "kor": "Korean", "chi": "Chinese", "chs": "Chinese",
              "cht": "Chinese"}
# Commentary and audio-description tracks: never the one to play by default.
EXTRA_AUDIO = re.compile(r"comment|коментар|комментар|director|descri|\bAD\b", re.I)
# Android caption locales for Just Player (it only tells languages apart, not the two Chinese scripts).
ANDROID_LOCALE = {"eng": "en", "spa": "es", "fre": "fr", "ita": "it", "ger": "de", "por": "pt", "jpn": "ja",
                  "kor": "ko", "chi": "zh", "chs": "zh_CN", "cht": "zh_TW"}


@dataclass(frozen=True)
class TrackPlan:
    audio: str | None              # language to switch audio to, if the file has it
    subtitles: list[str] | None    # languages to try in order; None = subtitles off


def original_country(meta: dict) -> str:
    return str(meta.get("country") or "").split(",")[0].strip().lower()


def plan(meta: dict, audio_language: str, subtitle_languages: list[str], native_language: str) -> TrackPlan:
    if original_country(meta) in NATIVE_COUNTRIES.get(native_language, set()):
        return TrackPlan(native_language, None)
    return TrackPlan(audio_language, subtitle_languages)


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
    """First language that has a full (not forced) track; plain before SDH."""
    for lang in langs:
        found = [t for t in tracks if matches(t, lang) and not t.get("isforced")]
        found.sort(key=lambda t: "sdh" in str(t.get("name") or "").lower())
        if found:
            return found[0]
    return None
