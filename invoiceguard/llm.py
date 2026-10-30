"""Claude client (Anthropic API or Amazon Bedrock in an EU region) with a cost meter around every call.

- LLM_PROVIDER=anthropic: Anthropic API with the user's key.
- LLM_PROVIDER=bedrock:   Amazon Bedrock in the company's AWS account (no key; the app's AWS role).

Every call's token counts come from the provider's reply; euros are list price x tokens.
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass

PRICES_CHECKED = "22 October 2026"
USD_TO_EUR = float(os.getenv("USD_TO_EUR", "0.89"))
PRICE_FACTOR = float(os.getenv("PRICE_FACTOR", "1"))  # 1.10 on Bedrock regional endpoints

# USD per 1M tokens: input, cache write, cache read, output (platform.claude.com/docs/en/about-claude/pricing)
PRICES = {
    "claude-opus-4-6": (5.00, 6.25, 0.50, 25.00),
    "claude-sonnet-4-6": (3.00, 3.75, 0.30, 15.00),
    "claude-haiku-4-5": (1.00, 1.25, 0.10, 5.00),
    "claude-opus-5-5": (4.00, 5.00, 0.20, 20.00),
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "scripted": (0.0, 0.0, 0.0, 0.0),  # the offline demo model
}
AGENT_MODELS = {
    "claude-sonnet-4-6": "Claude Sonnet 4.6 (default)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5 (cheapest)",
    "claude-haiku-4-5": "Claude Haiku 4.5",
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-opus-4-6": "Claude Opus 4.6",
}
BEDROCK_IDS = {
    "claude-haiku-4-5": "anthropic.claude-haiku-4-5",
    "claude-haiku-5-5": "anthropic.claude-haiku-5-5",
    "claude-sonnet-5-5": "anthropic.claude-sonnet-5-5",
    "claude-opus-5-5": "anthropic.claude-opus-5-5",
}


@dataclass
class Usage:
    model: str
    step: str
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def eur(self) -> float | None:
        price = next((v for k, v in PRICES.items() if self.model.startswith(k)), None)
        if price is None:
            return None
        usd = (
            self.input * price[0] + self.cache_write * price[1] + self.cache_read * price[2] + self.output * price[3]
        ) / 1e6
        return usd * USD_TO_EUR * PRICE_FACTOR

    def to_dict(self) -> dict:
        return {**asdict(self), "eur": self.eur}


class Meter:
    """Usage of every model call made through one client (thread-safe)."""

    def __init__(self):
        self.calls: list[Usage] = []
        self._lock = threading.Lock()

    def add(self, u: Usage) -> None:
        with self._lock:
            self.calls.append(u)

    def since(self, mark: int) -> list[Usage]:
        return list(self.calls[mark:])


def total(calls: list[Usage]) -> dict:
    eur = [c.eur for c in calls]
    return dict(
        calls=len(calls),
        input=sum(c.input + c.cache_read + c.cache_write for c in calls),
        output=sum(c.output for c in calls),
        eur=None if any(e is None for e in eur) else sum(eur),
    )


def eur(v: float | None) -> str:
    if v is None:
        return "price unknown"
    if v == 0:
        return "€0"
    if v < 0.0001:
        return "< €0.0001"
    return f"€{v:.4f}" if v < 0.01 else f"€{v:.3f}" if v < 1 else f"€{v:.2f}"


class MeteredClient:
    """Wraps an Anthropic client: same `messages.create`, every call counted in `meter`."""

    def __init__(self, client, meter: Meter | None = None, model_map: dict | None = None):
        self._client, self.meter, self._map = client, meter or Meter(), model_map or {}

    def create(self, step: str = "agent", **kwargs):
        model = kwargs["model"]
        resp = self._client.messages.create(**{**kwargs, "model": self._map.get(model, model)})
        u = getattr(resp, "usage", None)
        self.meter.add(
            Usage(
                model,
                step,
                input=getattr(u, "input_tokens", 0) or 0,
                output=getattr(u, "output_tokens", 0) or 0,
                cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            )
        )
        return resp


def make_client(
    provider: str = "anthropic", api_key: str | None = None, region: str = "eu-central-1", meter: Meter | None = None
) -> MeteredClient:
    if provider == "bedrock":
        from anthropic import AnthropicBedrockMantle

        return MeteredClient(AnthropicBedrockMantle(aws_region=region), meter, model_map=BEDROCK_IDS)
    import anthropic

    return MeteredClient(anthropic.Anthropic(api_key=api_key), meter)
