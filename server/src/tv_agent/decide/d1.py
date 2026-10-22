"""Client for Liquid AI's d1-3B decision model, served by llama.cpp's /v1/systemone endpoint.

d1 doesn't write text: given a state and named options it returns probabilities in one forward pass (about
100-300 ms on the Radeon 780M for a short prompt). The agent uses it to pick an intent and to choose between
candidate titles; it never sees or produces anything that reaches the TV directly.
"""
from dataclasses import dataclass

import httpx


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
    def __init__(self, http: httpx.AsyncClient, url: str):
        self.http, self.url = http, url.rstrip("/") + "/v1/systemone"

    async def ask(self, state, questions: dict, timeout: float = 3.0) -> dict:
        r = await self.http.post(self.url, json={"state": state, "questions": questions}, timeout=timeout)
        r.raise_for_status()
        return r.json()["answers"]

    async def choice(self, state, instructions: str, options: dict[str, str | None]) -> Choice:
        a = (await self.ask(state, {"q": {"type": "choice", "instructions": instructions, "criteria": options}}))["q"]
        return Choice(a["choice"], a.get("confidence", 0.0), a.get("probabilities", {}))

    async def yes(self, state, instructions: str) -> float:
        return (await self.ask(state, {"q": {"type": "noul", "instructions": instructions}}))["q"]["noul"]
