"""Descriptive analytics on received snapshots, never inferred chain-wide data."""

from __future__ import annotations

from statistics import median

WINDOW_MS = 15 * 60 * 1000
STALE_MS = 2 * 60 * 1000

_MARKET_BLOCKS = {
    "stale or future feed": "feed_stale",
    "invalid market": "invalid_market",
    "unsupported quote asset": "unsupported_quote",
    "unknown or out-of-range pair age": "pair_age",
    "liquidity or volume too low": "liquidity_volume",
    "mint re-entry cooldown": "reentry_cooldown",
}
_CAPITAL_BLOCKS = {
    "daily loss limit", "drawdown limit", "maximum open positions reached",
    "stale price for open position; new entries paused", "portfolio exposure cap reached",
    "virtual cash reserve protected", "capital risk budget too small",
    "estimated entry impact too high", "insufficient virtual cash", "fees exceed budget",
}


def classify_decision(action: str, reason: str) -> str:
    """One audit bucket per persisted decision; unknown reasons fail to 'other_*'."""
    if action == "SKIP":
        return "duplicate" if reason == "duplicate observation" else (
            "rl_declined" if reason == "RL declined entry" else "other")
    if action == "WATCH":
        return "watch_samples" if reason == "need earlier observed price" else "watch_momentum"
    if action == "BUY":
        return "entry"
    if action == "SELL":
        return "exit_sell"
    if action == "UNFILLED":
        return "exit_unfilled"
    if action == "HOLD":
        return "hold"
    if reason in _MARKET_BLOCKS:
        return _MARKET_BLOCKS[reason]
    if reason in _CAPITAL_BLOCKS:
        return "capital_guard"
    return "other_blocked"


def decision_funnel(rows: list[tuple], observations: int) -> dict:
    """rows: (action, reason, count) for one strategy version, one time window."""
    buckets: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for action, reason, count in rows:
        buckets[classify_decision(action, reason)] = buckets.get(classify_decision(action, reason), 0) + count
        reasons[reason] = reasons.get(reason, 0) + count
    evaluated = buckets.get("entry", 0) + buckets.get("rl_declined", 0)
    return {"observations": observations, "buckets": buckets, "evaluated": evaluated,
            "buy_proposed": buckets.get("entry", 0), "skip": buckets.get("rl_declined", 0),
            "top_reasons": [{"reason": r, "count": n} for r, n in
                            sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]}


def analyze_samples(samples: list[dict], now_ms: int, buys_5m: int, sells_5m: int,
                    created_at: int | None, quote_supported: bool) -> dict:
    """Samples are sorted by received_at and restricted to the last 15 minutes.

    A return or liquidity delta describes only the observed span; it is never
    labeled a fixed-horizon return when collection began later.
    """
    ordered = [s for s in samples if now_ms - WINDOW_MS <= s["received_at"] <= now_ms]
    ordered.sort(key=lambda s: s["received_at"])
    first, last = (ordered[0], ordered[-1]) if ordered else (None, None)
    intervals = [b["received_at"] - a["received_at"] for a, b in zip(ordered, ordered[1:])]
    intervals = [gap for gap in intervals if gap > 0]
    peak, drawdown = 0.0, 0.0
    for row in ordered:
        peak = max(peak, row["price"])
        if peak > 0:
            drawdown = max(drawdown, (peak - row["price"]) / peak * 100)
    total_txns = buys_5m + sells_5m
    flags = []
    if not last or now_ms - last["received_at"] > STALE_MS:
        flags.append("stale")
    if len(ordered) < 3:
        flags.append("sparse_samples")
    if created_at is None:
        flags.append("unknown_pair_age")
    if not quote_supported:
        flags.append("unsupported_quote")
    return {
        "window_minutes": 15,
        "sample_count": len(ordered),
        "observed_span_seconds": (last["received_at"] - first["received_at"]) / 1000 if len(ordered) > 1 else None,
        "median_gap_seconds": median(intervals) / 1000 if intervals else None,
        "price_return_pct": (last["price"] / first["price"] - 1) * 100 if len(ordered) > 1 else None,
        "liquidity_change_pct": (last["liquidity"] / first["liquidity"] - 1) * 100
                                if len(ordered) > 1 and first["liquidity"] > 0 else None,
        "sampled_max_drawdown_pct": drawdown if len(ordered) > 1 else None,
        "buy_share_5m_pct": buys_5m / total_txns * 100 if total_txns else None,
        "flags": flags,
        "series": [{"t": r["received_at"], "price": r["price"]} for r in ordered],
    }


def cohort_summary(markets: list[dict], now_ms: int, total_unique: int | None = None) -> dict:
    """Current displayed cohort only, never the full Solana universe."""
    return {
        "observed": len(markets),
        "historical_unique": total_unique,
        "fresh": sum(now_ms - m["received_at"] <= STALE_MS for m in markets),
        "supported_quote": sum("unsupported_quote" not in m["analysis"]["flags"] for m in markets),
        "blocked": sum(m["decision"] == "BLOCK" for m in markets),
        "rl_evaluated_latest": sum((m["params"] or {}).get("rl", {}).get("mode") == "experimental" for m in markets),
    }
