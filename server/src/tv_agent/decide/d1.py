"""Client for Liquid AI's d1-3B decision model, served by llama.cpp's /v1/systemone endpoint.

d1 doesn't write text: given a state and named options it returns probabilities in one forward pass (about
100-300 ms on the Radeon 780M for a short prompt). The agent uses it to pick an intent and to choose between
candidate titles; it never sees or produces anything that reaches the TV directly.

`where` picks which answers: the local d1, a hosted decision model, or one of them first and the other when the first
doesn't answer (stopped, crashed, busy, offline). The hosted one (`hosted_model`) is Liquid's own API for "d1" or
"d1-ultra" (`tv-agent setup liquid`), or any of OpenRouter's decision models by id ("typesafe/jev-1.14",
"cloudflare/clef", with the OpenRouter key): they all take the same request (System One). Liquid's terms let it use
what it's sent for training: what you said, and the options.
"""
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)
HOSTED = "https://api.liquid.ai/decisions/v1/systemone"
OPENROUTER = "https://openrouter.ai/api/alpha/decisions"
WHERE = {"local-first": ("local", "hosted"), "hosted-first": ("hosted", "local"), "local": ("local",),
         "hosted": ("hosted",)}


@dataclass(frozen=True)
class Choice:
    key: str
    confidence: float
    probabilities: dict[str, float]

    @property
    def margin(self) -> float:
        p = sorted(self.probabilities.values(), reverse=True)
        return p[0] - (p[1] if len(p) > 1 else 0.0)


class D1:
    def __init__(self, http: httpx.AsyncClient, url: str, hosted_key: str | None = None, hosted_model: str = "d1",
                 where: str = "local-first", openrouter_key: str | None = None):
        self.http, self.url = http, url.rstrip("/") + "/v1/systemone"
        self.hosted_key, self.hosted_model, self.where = hosted_key, hosted_model, where
        self.openrouter_key = openrouter_key
        self.last_source = "local"       # where the last answer came from: "local" or "hosted"
        self.last_usage: dict = {}       # the hosted answer's tokens and cost (OpenRouter reports the cost)

    def hosted(self) -> tuple[str, str] | None:
        """(endpoint, key) of the hosted decision model, or None without its key."""
        if "/" in self.hosted_model:
            return (OPENROUTER, self.openrouter_key) if self.openrouter_key else None
        return (HOSTED, self.hosted_key) if self.hosted_key else None

    def sources(self) -> tuple[str, ...]:
        """The decision models to ask, in order (the hosted one only with its key)."""
        return tuple(s for s in WHERE.get(self.where, WHERE["local-first"]) if s == "local" or self.hosted())

    async def ask(self, state, questions: dict, timeout: float = 3.0) -> dict:
        body = {"state": state, "questions": questions}
        sources = self.sources()
        if not sources:
            raise httpx.TransportError("no decision model to ask: the hosted one needs its key "
                                       "(tv-agent setup liquid, or openrouter for an OpenRouter model)")
        for i, source in enumerate(sources):
            try:
                if source == "local":
                    r = await self.http.post(self.url, json=body, timeout=timeout)
                else:
                    endpoint, key = self.hosted()
                    r = await self.http.post(endpoint, json={**body, "model": self.hosted_model}, timeout=timeout,
                                             headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                self.last_source = source
                data = r.json()
                self.last_usage = data.get("usage") or {}
                return data["answers"]
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if i == len(sources) - 1:
                    raise
                log.info("%s d1 didn't answer (%s); asking the %s one", source, type(e).__name__, sources[i + 1])
        raise AssertionError("unreachable")

    async def choice(self, state, instructions: str, options: dict[str, str | None]) -> Choice:
        a = (await self.ask(state, {"q": {"type": "choice", "instructions": instructions, "criteria": options}}))["q"]
        return Choice(a["choice"], a.get("confidence", 0.0), a.get("probabilities", {}))

    async def yes(self, state, instructions: str) -> float:
        return (await self.ask(state, {"q": {"type": "noul", "instructions": instructions}}))["q"]["noul"]
