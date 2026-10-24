"""A recorded command's whole trace, as the Decisions tab shows it under the diagram (stdlib only, so it's tested
without Qt): each step in the order it ended, its time and duration, how it was done (a badge: rule, LLM model, d1,
speech model, local algorithm, add-on, web, kept), how sure against its bar, what came of it and what it found; a
"details" link for what's in the command's own file (plots, subtitle passages, model prompts and answers); the
settings it ran with. Records from before the trace had these show what they have."""
import html
import json

# common.py's (it needs Qt; this doesn't)
GREEN, AMBER, RED, GREY, BLUE, PURPLE = "#2fb170", "#e0a030", "#e05555", "#8a8f98", "#4c8dff", "#9b6cf5"

HOW = ("<span style='color:gray'>How this trace was made: the agent recorded each step as it ran (server/tracing.py)."
       " The time is when the step ended, from when the command began (speech to text, before it, is negative);"
       " then how long it took, how it was done (the badge), how sure it was against the bar it had to pass, and what"
       " came of it. The decision's own steps are drawn above. What's too big for the history (plots, subtitle"
       " passages, the models' prompts and answers) is in the command's own file: “details”.</span>")
BADGE = {"rule": ("rule", GREEN), "llm": ("LLM", PURPLE), "d1": ("d1", BLUE), "stt": ("speech", "#30a0a0"),
         "local": ("local", GREY), "addon": ("add-on", AMBER), "web": ("web", AMBER), "cache": ("kept", GREY),
         "player": ("player", "#e07030")}
GOOD = ("accepted", "played", "play", "proven", "agreed", "done", "found", "said in it", "placed", "in time", "sent",
        "kept", "one exactly")
BAD = ("refused", "failed", "stopped", "none", "not placed", "not in time", "none of the words", "no line has the words")
# Labels of the steps recorded before they had their own (title and start steps of 2026-10-23)
OLD = {("title", "library"): "Your library", ("title", "search"): "Title search",
       ("title", "sound-alikes"): "LLM sound-alikes", ("title", "choice"): "Title chosen",
       ("title", "season check"): "Season check", ("title", "picked"): "Title", ("title", "episode"): "Episode found",
       ("start", "streams"): "Streams", ("start", "link check"): "Link check", ("start", "player"): "Player up",
       ("start", "load"): "Load", ("start", "first frame"): "First frame"}
SKIP = {"stage", "step", "label", "at", "ms", "method", "detail", "confidence", "threshold", "verdict", "why"}


def _secs(ms) -> str:
    return f"{ms / 1000:.2f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _value(v) -> str:
    if isinstance(v, list):
        if v and all(isinstance(x, dict) for x in v):
            return "; ".join(", ".join(f"{k} {x[k]}" for k in x) for x in v[:8]) + (" …" if len(v) > 8 else "")
        return ", ".join(str(x) for x in v[:12]) + (" …" if len(v) > 12 else "")
    if isinstance(v, dict):
        return ", ".join(f"{k} {x}" for k, x in list(v.items())[:12])
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def badge(method: dict | None) -> str:
    if not method:
        return ""
    name, color = BADGE.get(method.get("kind", ""), (method.get("kind", "?"), GREY))
    who = str(method.get("id") or "")
    if method.get("kind") in ("llm", "d1", "stt"):
        who = who.split("/")[-1]
    return (f"<span style='background:{color};color:white;border-radius:3px'>&nbsp;{html.escape(name)}&nbsp;</span>"
            + (f" <span style='color:{color}'>{html.escape(who)}</span>" if who else ""))


def verdict(v) -> str:
    if v is None:
        return ""
    text = str(v)
    color = GREEN if text in GOOD else RED if text in BAD else AMBER
    return f"<b style='color:{color}'>{html.escape(text)}</b>"


def step_line(t: dict, with_details: bool) -> str:
    label = t.get("label") or OLD.get((t.get("stage"), t.get("step"))) or str(t.get("step") or t.get("stage"))
    parts = [f"<span style='color:gray'>{t['at'] / 1000:+.2f} s</span>" if isinstance(t.get("at"), (int, float))
             else "", badge(t.get("method")), f"<b>{html.escape(label)}</b>"]
    if t.get("ms") is not None:
        parts.append(f"<span style='color:gray'>{_secs(t['ms'])}</span>")
    if t.get("confidence") is not None:
        c, bar = t["confidence"], t.get("threshold")
        sure = bar is None or c >= bar
        parts.append(f"<span style='color:{GREEN if sure else RED}'>confidence {_value(c)}"
                     + (f" {'≥' if sure else '&lt;'} {_value(bar)}" if bar is not None else "") + "</span>")
    elif t.get("threshold") is not None:
        parts.append(f"<span style='color:gray'>bar {_value(t['threshold'])}</span>")
    if t.get("verdict") is not None:
        parts.append("→ " + verdict(t["verdict"]))
    if t.get("why"):
        parts.append(f"<i>{html.escape(str(t['why']))}</i>")
    found = [f"{html.escape(k.replace('_', ' '))}: {html.escape(_value(v))[:240]}"
             for k, v in t.items() if k not in SKIP and v not in (None, "", [], {})]
    if found:
        parts.append("<span style='color:gray'>— " + " · ".join(found) + "</span>")
    if with_details and t.get("detail"):
        parts.append(f"<a href='detail:{html.escape(t['detail'])}'>details</a>")
    return " ".join(p for p in parts if p)


def settings_line(s: dict | None) -> str:
    if not s:
        return ""
    v = s.get("versions") or {}
    ver = " · ".join(f"{k.replace('_', ' ')} {x if not isinstance(x, list) else '/'.join(x)}" for k, x in v.items())
    sp, de, la = s.get("speech") or {}, s.get("deciding") or {}, s.get("languages") or {}
    parts = [ver, f"player {s.get('player', '?')}",
             f"speech {sp.get('where', '?')}: {sp.get('hosted_model') if sp.get('where') == 'hosted' else sp.get('model')}"
             + (f" ({sp['language']}, {sp.get('vocabulary', 0)} names)" if sp.get("language") else ""),
             "steps " + " → ".join(x + (" (off)" if x in (de.get("off") or []) else "") for x in de.get("steps") or []),
             f"LLM {str(de.get('llm', '?')).split('/')[-1]}", f"d1 {de.get('d1_where', '?')} {de.get('d1_hosted', '')}",
             f"audio {la.get('audio', '?')}, subtitles {la.get('subtitles', '?')} "
             f"{'/'.join(la.get('subtitle_languages') or [])}, native {la.get('native', '?')}"]
    return ("<b>Settings at the time</b> <a href='settings:'>all</a><br><span style='color:gray'>"
            + html.escape(" · ".join(p for p in parts if p)) + "</span>")


def timeline(entry: dict, has_details: bool) -> str:
    """The trace's timed steps of a recorded command (empty when it has none: older records)."""
    steps = sorted((t for t in entry.get("trace") or [] if isinstance(t, dict) and isinstance(t.get("at"), (int, float))),
                   key=lambda t: t["at"])
    if not steps and not entry.get("settings"):
        return ""
    lines = ["<b>The whole trace</b>"
             + (" <span style='color:gray'>(its details aren't kept any more)</span>"
                if entry.get("details") and not has_details else ""), HOW]
    lines += [step_line(t, has_details) for t in steps]
    if entry.get("settings"):
        lines.append(settings_line(entry["settings"]))
    return "<br>".join(lines)


def as_json(entry: dict, details: dict | None) -> str:
    """The record and its details, for "Copy as JSON"."""
    return json.dumps({"record": entry, "details": details}, ensure_ascii=False, indent=1)
