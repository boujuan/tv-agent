"""The closed set of things the agent can do. The decision layer (rules + d1-3B) only ever produces one of these
validated objects; nothing it outputs reaches the TV's shell except through the executors in agent.py."""
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


Title = Annotated[str, Field(min_length=1, max_length=120)]


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlayTitle(_Action):
    type: Literal["play_title"] = "play_title"
    title: Title | None = None           # None: the series playing now (or the last one watched)
    season: int | None = Field(None, ge=0, le=100)
    episode: int | None = Field(None, ge=0, le=2000)
    kind: Literal["movie", "series"] | None = None
    restart: bool = False
    latest: bool = False                 # "the latest episode of …": the newest one out


class ContinueWatching(_Action):
    type: Literal["continue_watching"] = "continue_watching"
    title: Title | None = None


class NextEpisode(_Action):
    type: Literal["next_episode"] = "next_episode"
    offset: int = Field(1, ge=-20, le=20)   # -1: the previous episode


class OpenInStremio(_Action):
    type: Literal["open_in_stremio"] = "open_in_stremio"
    title: Title
    season: int | None = Field(None, ge=0, le=100)
    episode: int | None = Field(None, ge=0, le=2000)
    kind: Literal["movie", "series"] | None = None


class YouTube(_Action):
    type: Literal["youtube"] = "youtube"
    query: Annotated[str, Field(min_length=1, max_length=200)]


class Twitch(_Action):
    type: Literal["twitch"] = "twitch"
    channel: Annotated[str, Field(min_length=1, max_length=50)]


class OpenApp(_Action):
    type: Literal["open_app"] = "open_app"
    app: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,30}$")]   # a key of the app list in the rules


class Home(_Action):
    type: Literal["home"] = "home"


class Transport(_Action):
    type: Literal["transport"] = "transport"
    op: Literal["pause", "resume", "stop", "forward", "back", "seek_to", "next", "previous"]
    seconds: int | None = Field(None, ge=0, le=14400)   # for forward/back/seek_to
    percent: int | None = Field(None, ge=0, le=100)     # seek_to a share of the runtime ("the middle")


class Volume(_Action):
    type: Literal["volume"] = "volume"
    op: Literal["up", "down", "set", "mute", "unmute"]
    amount: int | None = Field(None, ge=0, le=100)      # steps for up/down, level for set


Lang = Annotated[str, Field(pattern=r"^[a-z]{3}$")]   # ISO 639-2, as languages.py names them (chs/cht: Chinese)


class Binge(_Action):
    type: Literal["binge"] = "binge"
    on: bool                             # at the end of an episode, the next one starts by itself


class Timer(_Action):
    type: Literal["timer"] = "timer"
    minutes: int | None = Field(None, ge=1, le=600)   # None: when what's playing ends
    then: Literal["stop", "off"] = "stop"             # stop and go Home, or turn the TV off
    cancel: bool = False


class Tracks(_Action):
    type: Literal["tracks"] = "tracks"
    subtitles: Literal["on", "off"] | Lang | None = None
    audio: Literal["original"] | Lang | None = None


class Replay(_Action):
    type: Literal["replay"] = "replay"   # "what did they say?": back a little, subtitles on for a moment
    seconds: int = Field(10, ge=1, le=120)


class ShowList(_Action):
    type: Literal["show_list"] = "show_list"   # a numbered list on the TV; "number 2" plays from it
    list: Literal["recent", "new", "movies"]   # what you were watching; new episodes of your shows; new films


class Pick(_Action):
    type: Literal["pick"] = "pick"             # "number 2": from the list on screen
    number: int = Field(ge=1, le=20)


class LibraryEdit(_Action):
    type: Literal["library"] = "library"
    op: Literal["add", "remove", "watched", "unwatched"]
    title: Title | None = None                 # None: what's playing, else what you watched last
    season: int | None = Field(None, ge=0, le=100)
    episode: int | None = Field(None, ge=0, le=2000)
    kind: Literal["movie", "series"] | None = None


class Unclear(_Action):
    type: Literal["none"] = "none"
    heard: str = ""


Action = Annotated[Union[PlayTitle, ContinueWatching, NextEpisode, OpenInStremio, YouTube, Twitch, OpenApp, Home,
                         Transport, Volume, Binge, Timer, Tracks, Replay, ShowList, Pick, LibraryEdit, Unclear],
                   Field(discriminator="type")]
ACTION = TypeAdapter(Action)
ACTION_TYPES = {m.model_fields["type"].default: m for m in (PlayTitle, ContinueWatching, NextEpisode, OpenInStremio,
                                                             YouTube, Twitch, OpenApp, Home, Transport, Volume, Binge,
                                                             Timer, Tracks, Replay, ShowList, Pick, LibraryEdit,
                                                             Unclear)}


def parse(data: str | dict) -> Action:
    return ACTION.validate_json(data) if isinstance(data, str) else ACTION.validate_python(data)
