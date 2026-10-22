"""Client for Liquid AI's d1-3B decision model, served by llama.cpp's /v1/systemone endpoint.

d1 doesn't write text: given a state and named options it returns probabilities in one forward pass (about
100-300 ms on the Radeon 780M for a short prompt). The agent uses it to pick an intent and to choose between
candidate titles; it never sees or produces anything that reaches the TV directly.

When the local d1 doesn't answer (llama-d1 stopped, crashed or busy) and a Liquid API key is set
(`tv-agent setup liquid`, `hosted_d1` on), the same request goes to Liquid's hosted d1 (~0.2-0.3 s, a bigger model).
Liquid's terms let it use what it's sent for training: what you said, and the options.
"""
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)
HOSTED = "https://api.liquid.ai/decisions/v1/systemone"


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
    def __init__(self, http: httpx.AsyncClient, url: str, hosted_key: str | None = None, hosted_model: str = "d1"):
        self.http, self.url = http, url.rstrip("/") + "/v1/systemone"
        self.hosted_key, self.hosted_model = hosted_key, hosted_model
        self.last_source = "local"       # where the last answer came from: "local" or "hosted"

    async def ask(self, state, questions: dict, timeout: float = 3.0) -> dict:
        body = {"state": state, "questions": questions}
        try:
            r = await self.http.post(self.url, json=body, timeout=timeout)
            r.raise_for_status()
            self.last_source = "local"
            return r.json()["answers"]
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            if not self.hosted_key:
                raise
            log.info("local d1 didn't answer (%s); asking Liquid's hosted d1", type(e).__name__)
        r = await self.http.post(HOSTED, json={**body, "model": self.hosted_model}, timeout=timeout,
                                 headers={"Authorization": f"Bearer {self.hosted_key}"})
        r.raise_for_status()
        self.last_source = "hosted"
        return r.json()["answers"]

    async def choice(self, state, instructions: str, options: dict[str, str | None]) -> Choice:
        a = (await self.ask(state, {"q": {"type": "choice", "instructions": instructions, "criteria": options}}))["q"]
        return Choice(a["choice"], a.get("confidence", 0.0), a.get("probabilities", {}))

    async def yes(self, state, instructions: str) -> float:
        return (await self.ask(state, {"q": {"type": "noul", "instructions": instructions}}))["q"]["noul"]
