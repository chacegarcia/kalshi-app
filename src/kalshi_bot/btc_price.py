"""Spot crypto/USD from public APIs (no key) for logging / context next to Kalshi crypto markets."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

# CoinGecko public endpoint; rate limits apply — keep poll intervals reasonable (e.g. ≥15s).
_COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
_BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price?symbol={symbol}"


def coingecko_id_for_kalshi_ticker(ticker: str) -> str:
    """Map Kalshi series prefix to CoinGecko ``ids`` (lowercase slug)."""
    u = (ticker or "").strip().upper()
    if u.startswith("KXETH"):
        return "ethereum"
    return "bitcoin"


def binance_symbol_for_kalshi_ticker(ticker: str) -> str:
    """USDT pair on Binance public API for spot reference."""
    u = (ticker or "").strip().upper()
    if u.startswith("KXETH"):
        return "ETHUSDT"
    return "BTCUSDT"


def fetch_spot_usd_coingecko(coin_id: str) -> float | None:
    """Single-id CoinGecko simple price."""
    cid = (coin_id or "bitcoin").strip().lower()
    if not cid:
        return None
    url = _COINGECKO_SIMPLE.format(coin_id=cid)
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "kalshi-trading-bot/1.0"},
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            data = json.loads(resp.read().decode())
        block = data.get(cid) or data.get(coin_id) or next(iter(data.values()), None)
        if not isinstance(block, dict):
            return None
        raw = block.get("usd")
        if raw is None:
            return None
        return float(raw)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TypeError, ValueError, KeyError, StopIteration):
        return None


def fetch_spot_usd_binance_symbol(symbol: str) -> float | None:
    """Binance public last price for a USDT pair (e.g. BTCUSDT, ETHUSDT)."""
    sym = (symbol or "BTCUSDT").strip().upper()
    url = _BINANCE_TICKER.format(symbol=sym)
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "kalshi-trading-bot/1.0"},
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read().decode())
        raw = data.get("price")
        if raw is None:
            return None
        return float(raw)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TypeError, ValueError, KeyError):
        return None


def fetch_btc_usd_spot() -> float | None:
    """Return approximate BTC/USD spot (CoinGecko only). Kept for backward compatibility."""
    return fetch_spot_usd_coingecko("bitcoin")


def fetch_crypto_spot_usd_for_kalshi_ticker(
    kalshi_ticker: str,
    source: str = "auto",
) -> tuple[float | None, str]:
    """Reference spot USD + label for logging next to a Kalshi crypto contract.

    ``source``: ``auto`` (CoinGecko then Binance), ``coingecko``, or ``binance``.
    Chooses BTC vs ETH from ticker prefix (``KXETH`` → ETH; else BTC).
    """
    src = (source or "auto").strip().lower()
    if src not in ("auto", "coingecko", "binance"):
        src = "auto"
    cid = coingecko_id_for_kalshi_ticker(kalshi_ticker)
    sym = binance_symbol_for_kalshi_ticker(kalshi_ticker)

    def _cg() -> tuple[float | None, str]:
        p = fetch_spot_usd_coingecko(cid)
        return p, f"coingecko:{cid}"

    def _bn() -> tuple[float | None, str]:
        p = fetch_spot_usd_binance_symbol(sym)
        return p, f"binance:{sym}"

    if src == "coingecko":
        return _cg()
    if src == "binance":
        return _bn()
    p, lab = _cg()
    if p is not None:
        return (p, lab)
    p, lab = _bn()
    return (p, lab)
