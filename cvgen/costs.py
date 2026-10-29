"""What each CV costs: per-call rows, totals per step, output/costs.csv, and reports.

Every API call becomes one row (call_row): step, model, effort, token counts, web searches,
and cost in USD. Rows are stored in cv.meta.json["calls"]; append_csv() adds one summary line
per CV to output/costs.csv; report() and bench_table() aggregate them.
"""

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

# USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing
# (checked 2026-10-29; Anthropic first-party API, global routing; US-only inference_geo is 1.1x).
# model: (input, 5-minute cache write, 1-hour cache write, cache read, output)
PRICES_CHECKED = "2026-10-29"
PRICES = {
    "claude-fable-5-1": (10.00, 12.50, 20.00, 0.25, 50.00),
    "claude-mythos-5-1": (10.00, 12.50, 20.00, 0.25, 50.00),
    "claude-fable-5": (10.00, 12.50, 20.00, 1.00, 50.00),
    "claude-mythos-5": (10.00, 12.50, 20.00, 1.00, 50.00),
    "claude-opus-5-5": (4.00, 5.00, 8.00, 0.20, 20.00),
    "claude-opus-5": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-8": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-7": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-6": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-5": (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-sonnet-5-5": (2.00, 2.50, 4.00, 0.10, 10.00),
    "claude-sonnet-5": (2.00, 2.50, 4.00, 0.20, 10.00),
    "claude-sonnet-4-6": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-sonnet-4-5": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),  # prompts up to 100,000 tokens
    "claude-haiku-4-5": (1.00, 1.25, 2.00, 0.10, 5.00),
}
HAIKU_5_5_LONG = (0.50, 0.625, 1.00, 0.05, 2.50)  # Haiku 5.5 prompts over 100,000 tokens (incl. cache reads/writes)
WEB_SEARCH_PER_1000 = 10.00  # plus the search results, which are billed as input tokens

TOKEN_FIELDS = ("input_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_read_tokens", "output_tokens")
NUMERIC_FIELDS = TOKEN_FIELDS + ("web_searches", "cost_usd")
BUCKETS = ("tailor", "repair", "layout", "gap_report")
CSV_FIELDS = ["date", "folder", "model", "calls", "total_usd"] + [f"{b}_usd" for b in BUCKETS]


def _model_key(model: str) -> str | None:
    return next((m for m in sorted(PRICES, key=len, reverse=True) if (model or "").startswith(m)), None)


def prices_for(model: str, prompt_tokens: int = 0) -> tuple | None:
    key = _model_key(model)
    if key == "claude-haiku-5-5" and prompt_tokens > 100_000:
        return HAIKU_5_5_LONG
    return PRICES.get(key) if key else None


def row_cost(row: dict) -> float | None:
    prompt = row["input_tokens"] + row["cache_write_5m_tokens"] + row["cache_write_1h_tokens"] + row["cache_read_tokens"]
    prices = prices_for(row["model"], prompt)
    if prices is None:
        return None
    inp, w5, w1, read, out = prices
    tokens = (row["input_tokens"] * inp + row["cache_write_5m_tokens"] * w5 + row["cache_write_1h_tokens"] * w1
              + row["cache_read_tokens"] * read + row["output_tokens"] * out) / 1_000_000
    return round(tokens + row["web_searches"] * WEB_SEARCH_PER_1000 / 1000, 6)


def call_row(step: str, message, effort: str | None) -> dict:
    """One row per API call, from the response's usage."""
    u = message.usage
    written = getattr(u, "cache_creation_input_tokens", 0) or 0
    split = getattr(u, "cache_creation", None)
    w1h = int(getattr(split, "ephemeral_1h_input_tokens", 0) or 0) if split is not None else 0
    server = getattr(u, "server_tool_use", None)
    row = {
        "step": step,
        "model": getattr(message, "model", "") or "",
        "effort": effort,
        "input_tokens": int(u.input_tokens or 0),
        "cache_write_5m_tokens": int(written) - w1h,  # the pipeline only uses the default 5-minute TTL
        "cache_write_1h_tokens": w1h,
        "cache_read_tokens": int(getattr(u, "cache_read_input_tokens", 0) or 0),
        "output_tokens": int(u.output_tokens or 0),
        "web_searches": int(getattr(server, "web_search_requests", 0) or 0) if server is not None else 0,
    }
    row["cost_usd"] = row_cost(row)
    return row


def totals(rows: list[dict]) -> dict:
    """Sum of the numeric fields (cost is None if any row's model is unpriced)."""
    out = {k: sum(r.get(k) or 0 for r in rows) for k in NUMERIC_FIELDS}
    out["cost_usd"] = None if any(r.get("cost_usd") is None for r in rows) else round(out["cost_usd"], 6)
    out["calls"] = len(rows)
    return out


def bucket(step: str) -> str:
    if step.startswith("layout"):
        return "layout"
    if step.startswith("gap"):
        return "gap_report"
    return "repair" if step == "repair" else "tailor"


def by_bucket(rows: list[dict]) -> dict[str, float]:
    out = {b: 0.0 for b in BUCKETS}
    for r in rows:
        out[bucket(r["step"])] += r.get("cost_usd") or 0
    return {b: round(v, 6) for b, v in out.items()}


def fmt(cost: float | None) -> str:
    return "n/a" if cost is None else f"${cost:.2f}"


# ---------------------------
# CSV AND REPORTS
# ---------------------------
def append_csv(path: Path, folder: str, model: str, rows: list[dict], day: date | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.is_file()
    total = totals(rows)["cost_usd"]
    line = {"date": (day or date.today()).isoformat(), "folder": folder, "model": model, "calls": len(rows),
            "total_usd": "" if total is None else f"{total:.6f}",
            **{f"{b}_usd": f"{v:.6f}" for b, v in by_bucket(rows).items()}}
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(line)


def read_csv(path: Path) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def report(lines: list[dict], days: int, today: date | None = None) -> dict:
    """Average cost per CV and per step over the last `days` days (today included)."""
    since = (today or date.today()) - timedelta(days=days - 1)
    recent = [l for l in lines if l.get("total_usd") and date.fromisoformat(l["date"]) >= since]
    n = len(recent)
    avg = lambda key: (sum(float(l.get(key) or 0) for l in recent) / n) if n else None
    return {"days": days, "cvs": n, "total": sum(float(l["total_usd"]) for l in recent),
            "per_cv": avg("total_usd"), "per_step": {b: avg(f"{b}_usd") for b in BUCKETS}}


def bench_table(rows: list[dict]) -> list[dict]:
    """One line per step (layout rounds kept separate), sorted by cost, most expensive first."""
    steps = {}
    for r in rows:
        steps.setdefault(r["step"], []).append(r)
    table = [{"step": step, **totals(group)} for step, group in steps.items()]
    return sorted(table, key=lambda t: -(t["cost_usd"] or 0))


def cost_drivers(rows: list[dict], top: int = 3) -> list[tuple[str, str, float]]:
    """(step, component, USD) for the largest single components: output, cache writes, input, cache reads, searches."""
    parts = {}
    for r in rows:
        prompt = r["input_tokens"] + r["cache_write_5m_tokens"] + r["cache_write_1h_tokens"] + r["cache_read_tokens"]
        prices = prices_for(r["model"], prompt)
        if prices is None:
            continue
        inp, w5, w1, read, out = prices
        for name, usd in (("output tokens (incl. thinking)", r["output_tokens"] * out / 1e6),
                          ("cache writes", (r["cache_write_5m_tokens"] * w5 + r["cache_write_1h_tokens"] * w1) / 1e6),
                          ("uncached input", r["input_tokens"] * inp / 1e6),
                          ("cache reads", r["cache_read_tokens"] * read / 1e6),
                          ("web searches", r["web_searches"] * WEB_SEARCH_PER_1000 / 1000)):
            key = (r["step"], name)
            parts[key] = parts.get(key, 0) + usd
    return sorted(((s, n, round(v, 4)) for (s, n), v in parts.items()), key=lambda x: -x[2])[:top]
