"""Spot BTC/USD from a public API (no API key) for logging / context next to Kalshi market data."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

# CoinGecko public endpoint; rate limits apply — keep poll intervals reasonable (e.g. ≥15s).
_COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"


def fetch_btc_usd_spot() -> float | None:
    """Return approximate BTC/USD spot, or None if the request fails."""
    try:
        req = urllib.request.Request(
            _COINGECKO_SIMPLE,
            headers={"Accept": "application/json", "User-Agent": "kalshi-trading-bot/1.0"},
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            data = json.loads(resp.read().decode())
        raw = data.get("bitcoin", {}).get("usd")
        if raw is None:
            return None
        return float(raw)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TypeError, ValueError, KeyError):
        return None
