"""DEX Screener discovery and point-in-time market snapshots."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

BASE_URL = "https://api.dexscreener.com"
SUPPORTED_QUOTES = {
    "So11111111111111111111111111111111111111112",  # wrapped SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
}


@dataclass(frozen=True)
class Snapshot:
    mint: str
    pair: str
    received_at: int  # UTC milliseconds; never replace with pairCreatedAt
    created_at: int | None
    price: float
    liquidity: float
    volume_5m: float
    buys_5m: int
    sells_5m: int
    raw: str


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def parse_pair(mint: str, data: dict[str, Any], received_at: int) -> Snapshot | None:
    if data.get("chainId") != "solana" or data.get("baseToken", {}).get("address") != mint:
        return None  # Quoted token prices are not the requested token's USD price.
    price = _positive(data.get("priceUsd"))
    liquidity = _positive((data.get("liquidity") or {}).get("usd"))
    pair = data.get("pairAddress")
    if price is None or liquidity is None or not isinstance(pair, str) or not pair:
        return None
    created = data.get("pairCreatedAt")
    created = created if isinstance(created, int) and 0 < created <= received_at else None
    volume = (data.get("volume") or {}).get("m5", 0)
    txns = (data.get("txns") or {}).get("m5") or {}
    try:
        volume = float(volume)
        buys, sells = int(txns.get("buys", 0)), int(txns.get("sells", 0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(volume) or volume < 0 or buys < 0 or sells < 0:
        return None
    return Snapshot(mint, pair, received_at, created, price, liquidity, volume, buys, sells,
                    json.dumps(data, separators=(",", ":"), sort_keys=True))


class DexScreener:
    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def get(self, path: str) -> Any:
        request = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "meme-paper/0.1", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def snapshots(self, received_at: int | None = None, limit: int = 15,
                  tracked_pairs: dict[str, str] | None = None,
                  discovered_mints: list[str] | None = None) -> list[Snapshot]:
        # Profiles are a biased, incomplete candidate source, not a new-pool feed.
        profiles = self.get("/token-profiles/latest/v1")
        if not isinstance(profiles, list):
            raise ValueError("Unexpected profiles response")
        mints: list[str] = []
        for item in profiles:
            if isinstance(item, dict) and item.get("chainId") == "solana":
                mint = item.get("tokenAddress")
                if isinstance(mint, str) and 32 <= len(mint) <= 44 and mint not in mints:
                    mints.append(mint)
            if len(mints) >= min(limit, 30):
                break
        for mint in (discovered_mints or [])[:15]:
            if mint not in mints:
                mints.append(mint)
        tracked_pairs = tracked_pairs or {}
        for mint in tracked_pairs:
            if mint not in mints:
                mints.append(mint)
        if not mints:
            return []
        batches = [mints[start:start + 15] for start in range(0, len(mints), 15)]

        def fetch(batch_mints: list[str]):
            batch = self.get("/tokens/v1/solana/" + ",".join(batch_mints))
            if not isinstance(batch, list):
                raise ValueError("Unexpected token pairs response")
            # Timestamp at availability, not before the concurrent HTTP request starts.
            seen_at = received_at if received_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
            return [(item, seen_at) for item in batch if isinstance(item, dict)]

        # I/O and independent token normalization run concurrently. Ledger writes remain serialized.
        with ThreadPoolExecutor(max_workers=min(3, len(batches))) as executor:
            results = list(executor.map(fetch, batches))
        candidates: dict[str, list[Snapshot]] = {}
        for batch_items in results:
            for item, seen_at in batch_items:
                mint = (item.get('baseToken') or {}).get('address')
                if mint not in mints:
                    continue
                snap = parse_pair(mint, item, seen_at)
                if snap:
                    candidates.setdefault(mint, []).append(snap)
        selected: list[Snapshot] = []
        for mint in mints:
            pool = candidates.get(mint, [])
            if not pool:
                continue
            chosen = next((s for s in pool if s.pair == tracked_pairs.get(mint)), None)
            if chosen is None:
                # Tradeable pools (wSOL/USDC quoted) beat higher-liquidity meme-quoted pools;
                # an unsupported pool stays selectable for observation only when nothing else exists.
                supported = [s for s in pool
                             if (json.loads(s.raw).get('quoteToken') or {}).get('address') in SUPPORTED_QUOTES]
                chosen = max(supported or pool, key=lambda s: s.liquidity)
            selected.append(chosen)
        return selected
