from agent.pricing import TokenUsage, estimate_cost_usd


def test_estimate_cost_uses_default_model_prices():
    estimate = estimate_cost_usd("gpt-4.1-mini", 1_000_000, 1_000_000)

    assert estimate["estimated_cost_usd"] == 2.0
    assert estimate["cost_source"] == "default_estimate"


def test_token_usage_accumulates_and_prices():
    usage = TokenUsage(provider="openai", model="gpt-4.1-mini")
    usage.add(input_tokens=1000, output_tokens=500)

    saved = usage.as_dict()
    assert saved["total_tokens"] == 1500
    assert saved["estimated_cost_usd"] is not None
