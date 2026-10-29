"""Estimated API cost of a run, from token usage.

Prices are USD per million tokens (Anthropic first-party API, checked 2026-10). Cache
writes use the 5-minute TTL rate (1.25x input); cache reads use each model's read rate.
Thinking tokens are billed as output and are already in output_tokens. It's an estimate:
a server-side refusal fallback may bill some tokens at another model's rate.
"""

# model: (input, output, cache read)
PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
CACHE_WRITE_MULTIPLIER = 1.25


def estimate(usage: dict, model: str) -> float | None:
    """USD for one usage dict ({input_tokens, output_tokens, cache_*}); None for an unknown model."""
    prices = PRICES.get(model)
    if prices is None or not usage:
        return None if prices is None else 0.0
    inp, out, read = prices
    return (
        usage.get("input_tokens", 0) * inp
        + usage.get("output_tokens", 0) * out
        + usage.get("cache_creation_input_tokens", 0) * inp * CACHE_WRITE_MULTIPLIER
        + usage.get("cache_read_input_tokens", 0) * read
    ) / 1_000_000


WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches, on top of the tokens their results add


def run_cost(meta: dict) -> float | None:
    """Total for a pipeline run from cv.meta.json: tailoring, layout and gap-report calls."""
    model = meta.get("model", "")
    model = next((m for m in sorted(PRICES, key=len, reverse=True) if model.startswith(m)), model)
    gap_usage = (meta.get("gap_report") or {}).get("usage", {})
    parts = [estimate(meta.get("usage", {}), model), estimate(meta.get("layout", {}).get("usage", {}), model),
             estimate(gap_usage, model)]
    if any(p is None for p in parts):
        return None
    return round(sum(parts) + gap_usage.get("web_search_requests", 0) * WEB_SEARCH_USD, 4)


def fmt(cost: float | None) -> str:
    return "n/a" if cost is None else f"${cost:.2f}"
