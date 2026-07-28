from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_MODEL_PRICES_USD_PER_1M: dict[str, dict[str, float]] = {
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.4, "output": 1.6},
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "claude-3-5-sonnet-latest": {"input": 3.0, "output": 15.0},
    "claude-3-7-sonnet-latest": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
}


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    prices: Mapping[str, Mapping[str, float]] | None = None,
    *,
    source: str = "default_estimate",
) -> dict[str, Any]:
    configured = prices or DEFAULT_MODEL_PRICES_USD_PER_1M
    price = configured.get(model) or configured.get(model.lower())
    if not price:
        return {"estimated_cost_usd": None, "cost_source": "unpriced"}
    cost = (input_tokens / 1_000_000 * float(price["input"])) + (
        output_tokens / 1_000_000 * float(price["output"])
    )
    return {"estimated_cost_usd": round(cost, 6), "cost_source": source}


@dataclass(slots=True)
class TokenUsage:
    provider: str
    model: str
    prices: Mapping[str, Mapping[str, float]] | None = None
    price_source: str = "default_estimate"
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = None
    cost_source: str = "unpriced"

    def add(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens = self.input_tokens + self.output_tokens
        estimate = estimate_cost_usd(
            self.model,
            self.input_tokens,
            self.output_tokens,
            self.prices,
            source=self.price_source,
        )
        self.estimated_cost_usd = estimate["estimated_cost_usd"]
        self.cost_source = estimate["cost_source"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "cost_source": self.cost_source,
        }
