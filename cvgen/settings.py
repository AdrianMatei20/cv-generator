"""Model, effort and cost settings, read from the environment (and .env, loaded by the pipeline).

Each setting can be changed per machine in .env; the defaults are what `cvgen bench` showed to
be cheapest at the same CV quality (see CLAUDE.md, "Cost settings"). Read them through these
functions at request time, never at import, so tests and the CLI can override them.
"""

import os

DEFAULTS = {
    "TAILOR_MODEL": "claude-opus-5-5",   # the CV itself; CV_MODEL is the older name
    "TAILOR_EFFORT": "medium",           # CV_EFFORT is the older name; low saved little once its repairs were paid (bench 4)
    "GAP_MODEL": "claude-opus-5-5",      # Sonnet 5.5 was cheaper but judged gaps far more leniently (bench 1-2)
    "GAP_EFFORT": "medium",              # as strict as high on must-haves, 20% cheaper (bench 4)
    "GAP_WEB_SEARCH": "0",               # opt-in: halves the gap report's cost; links become search links (bench 8)
    "GAP_MAX_SEARCHES": "3",             # only with GAP_WEB_SEARCH=1
    "LAYOUT_MODEL": "claude-haiku-5-5",
    "LAYOUT_EFFORT": "medium",
    "CACHE_TTL": "5m",                   # "5m" or "1h": TTL of the cached profile prefix
}
ALIASES = {"TAILOR_MODEL": "CV_MODEL", "TAILOR_EFFORT": "CV_EFFORT"}


def get(name: str) -> str:
    for key in (name, ALIASES.get(name)):
        value = os.environ.get(key) if key else None
        if value and value.strip():
            return value.strip()
    return DEFAULTS[name]


def tailor_model() -> str:
    return get("TAILOR_MODEL")


def tailor_effort() -> str:
    return get("TAILOR_EFFORT")


def gap_model() -> str:
    return get("GAP_MODEL")


def gap_effort() -> str:
    return get("GAP_EFFORT")


def gap_max_searches() -> int:
    return int(get("GAP_MAX_SEARCHES"))


def layout_model() -> str:
    return get("LAYOUT_MODEL")


def layout_effort() -> str:
    return get("LAYOUT_EFFORT")


def cache_control() -> dict:
    ttl = get("CACHE_TTL")
    if ttl not in ("5m", "1h"):
        raise ValueError(f"CACHE_TTL must be 5m or 1h, not {ttl!r}")
    return {"type": "ephemeral"} if ttl == "5m" else {"type": "ephemeral", "ttl": "1h"}


def supports_fallbacks(model: str) -> bool:
    """Server-side refusal fallback ("fallbacks": "default"): not available for Haiku models."""
    return not model.startswith("claude-haiku")
