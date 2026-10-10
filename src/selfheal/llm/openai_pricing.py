"""Best-effort local cost estimates for supported OpenAI chat models."""

from __future__ import annotations

from collections.abc import Mapping

MODEL_PRICES_PER_MILLION_USD: Mapping[str, tuple[float, float]] = {
    # input, output
    "gpt-3.5-turbo": (0.50, 1.50),
    "gpt-4": (30.00, 60.00),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4o": (5.00, 15.00),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return a local estimate, or zero when the model is not catalogued."""
    prices = MODEL_PRICES_PER_MILLION_USD.get(model)
    if prices is None:
        return 0.0
    input_price, output_price = prices
    return round(
        input_tokens * input_price / 1_000_000 + output_tokens * output_price / 1_000_000,
        6,
    )


class OpenAIPricing:
    """Compatibility wrapper for callers that use the original pricing class."""

    @staticmethod
    def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
        """Return the local estimate for one API response."""
        return estimate_cost_usd(model, input_tokens, output_tokens)
