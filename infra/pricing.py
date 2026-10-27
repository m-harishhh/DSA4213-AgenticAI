"""Price table for cost accounting: USD per million tokens, text modality, paid standard tier.

Source: https://ai.google.dev/gemini-api/docs/pricing, checked 20 Oct 2026. Thinking
tokens are billed at the output rate on every listed model. Re-check before paid runs
and copy the returned dict (with its basis) into game_start.pricing so each log records
the basis it was costed on. The proposal's 28 Sep 2026 figures for 2.5 and 3.1 Flash-Lite
are unchanged.
"""

PRICING_BASIS = ("ai.google.dev/gemini-api/docs/pricing, paid standard tier, text tokens, USD, "
                 "checked 20 Oct 2026; thinking tokens billed at the output rate")

PRICES = {
    "gemini-2.5-flash-lite": {"input_per_million": 0.10, "output_per_million": 0.40},
    "gemini-3.1-flash-lite": {"input_per_million": 0.25, "output_per_million": 1.50},
    "gemini-3.5-flash-lite": {"input_per_million": 0.30, "output_per_million": 2.50},
    # Same nominal model as gemini-3.1-flash-lite above, routed through OpenRouter
    # (infra.llm.OpenRouterBackend) instead of calling Google directly -- kept as a
    # distinct entry, not folded into the row above, because OpenRouter's own per-token
    # price for this route is higher than Google's direct price (its margin on top).
    # Source: https://openrouter.ai/google/gemini-3.1-flash-lite, checked 26 Oct 2026.
    "google/gemini-3.1-flash-lite": {"input_per_million": 0.275, "output_per_million": 1.65},
}


def pricing_for(model_id):
    """The LLMClient pricing dict for a model, or a KeyError naming the known models."""
    if model_id not in PRICES:
        raise KeyError(f"No price on file for {model_id!r}; known models: {sorted(PRICES)}. "
                       "Add it to infra/pricing.py with the date checked, or pass pricing=None for mocks.")
    prices = PRICES[model_id]
    return {"input_per_million": prices["input_per_million"],
            "output_per_million": prices["output_per_million"],
            "reasoning_per_million": prices["output_per_million"],
            "basis": PRICING_BASIS, "model_id": model_id}


def estimate_run_cost(usage_per_game, games, pricing):
    """USD for `games` games given one game's usage dict; a planning helper for the pilot estimate."""
    from .llm import cost_usd
    per_game = cost_usd(usage_per_game, pricing)
    return None if per_game is None else per_game * games
