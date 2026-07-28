"""Compatibility facade for domain pricing with environment configuration."""

from __future__ import annotations

import json
import os
from typing import Any

from agent.domain.pricing import DEFAULT_MODEL_PRICES_USD_PER_1M
from agent.domain.pricing import TokenUsage as DomainTokenUsage
from agent.domain.pricing import estimate_cost_usd as estimate_domain_cost


def configured_prices() -> dict[str, dict[str, float]]:
    prices = dict(DEFAULT_MODEL_PRICES_USD_PER_1M)
    raw = os.getenv("AGENT_MODEL_PRICES_JSON")
    if not raw:
        return prices
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return prices
    if not isinstance(loaded, dict):
        return prices
    for model, value in loaded.items():
        if isinstance(value, dict) and "input" in value and "output" in value:
            prices[str(model)] = {"input": float(value["input"]), "output": float(value["output"])}
    return prices


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
    source = "env" if os.getenv("AGENT_MODEL_PRICES_JSON") else "default_estimate"
    return estimate_domain_cost(model, input_tokens, output_tokens, configured_prices(), source=source)


class TokenUsage(DomainTokenUsage):
    def __init__(self, provider: str, model: str, **values: Any) -> None:
        super().__init__(
            provider=provider,
            model=model,
            prices=configured_prices(),
            price_source="env" if os.getenv("AGENT_MODEL_PRICES_JSON") else "default_estimate",
            **values,
        )


__all__ = ["DEFAULT_MODEL_PRICES_USD_PER_1M", "TokenUsage", "configured_prices", "estimate_cost_usd"]
