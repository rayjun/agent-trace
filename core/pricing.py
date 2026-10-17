"""Model pricing registry — tokens are the truth, cost is derived when known.

Proposal §6.4: if a model has no configured price, report token counts and
mark cost UNKNOWN — never an estimated price masquerading as a bill. Entries
carry an `as_of` date because list prices change; a stale entry is visibly
stale rather than silently wrong.
"""

from __future__ import annotations

# USD per 1M tokens. Public list prices, as_of 2025-08.
PRICES: dict[str, dict[str, float | str]] = {
    "gpt-4o":       {"input": 2.50, "output": 10.00, "cached_input": 1.25, "as_of": "2025-08"},
    "gpt-4o-mini":  {"input": 0.15, "output": 0.60, "cached_input": 0.075, "as_of": "2025-08"},
    "gpt-5":        {"input": 1.25, "output": 10.00, "cached_input": 0.125, "as_of": "2025-08"},
    "gpt-5-mini":   {"input": 0.25, "output": 2.00, "cached_input": 0.025, "as_of": "2025-08"},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00, "cached_input": 0.30, "as_of": "2025-08"},
    "claude-opus-4":   {"input": 15.00, "output": 75.00, "cached_input": 1.50, "as_of": "2025-08"},
}


def _lookup(model: str | None) -> dict[str, float | str] | None:
    if not model:
        return None
    if model in PRICES:
        return PRICES[model]
    # longest-prefix match: gpt-4o-2024-08-06 -> gpt-4o
    best = None
    for name, entry in PRICES.items():
        if model.startswith(name) and (best is None or len(name) > len(best[0])):
            best = (name, entry)
    return best[1] if best else None


def cost_usd(model: str | None, input_tokens: int | None,
             output_tokens: int | None,
             cache_read: int | None = None) -> float | None:
    """Cost in USD, or None when it cannot be known.

    None means UNKNOWN and covers three cases, all of which previously
    produced a confident number:
      * no configured price for the model;
      * no token figures at all (a run that measured nothing is not a $0 run
        — that is "an estimated price masquerading as a bill", the thing this
        module exists to prevent);
      * the inputs are inconsistent (cache_read exceeding input), which would
        otherwise compute a NEGATIVE cost via the cached-rate delta.

    Tokens are clamped at zero: a provider reporting a negative count is a
    data error, and a negative bill is worse than no bill.
    """
    price = _lookup(model)
    if price is None:
        return None
    if input_tokens is None and output_tokens is None and cache_read is None:
        return None
    in_tok = max(0, input_tokens or 0)
    out_tok = max(0, output_tokens or 0)
    cached = max(0, cache_read or 0)
    cost = (in_tok * float(price["input"])
            + out_tok * float(price["output"])) / 1_000_000
    # Cached tokens are a SUBSET of input billed at a lower rate: re-bill that
    # share at cached_input instead of input. Only when the subset relation
    # actually holds — the v1 trace history contains records where it does not
    # (see schema/CHANGELOG.md), and applying the delta anyway yields a
    # negative price.
    if cached and "cached_input" in price and cached <= in_tok:
        cost += (cached * (float(price["cached_input"])
                           - float(price["input"]))) / 1_000_000
    return round(max(0.0, cost), 6)


def price_info(model: str | None) -> dict[str, float | str] | None:
    return _lookup(model)
