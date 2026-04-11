"""REST helpers for markets and order books."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kalshi_python_sync.models.get_market_orderbook_response import GetMarketOrderbookResponse
from kalshi_python_sync.models.get_market_response import GetMarketResponse
from kalshi_python_sync.models.get_markets_response import GetMarketsResponse

from kalshi_bot.client import KalshiSdkClient, with_rest_retry


@dataclass
class MarketSummary:
    ticker: str
    title: str
    status: str | None
    volume: int | None


@with_rest_retry
def list_open_markets(
    client: KalshiSdkClient,
    *,
    limit: int = 50,
    mve_filter: str | None = "exclude",
) -> GetMarketsResponse:
    """List open markets.

    ``mve_filter='exclude'`` (default) drops multivariate / combo legs so the first page
    is usually normal binary markets with resting liquidity. MVE tickers often return
    empty YES/NO books via REST even when ``status=open``.
    Pass ``mve_filter=None`` to use the API default (includes MVE).
    """
    kwargs: dict[str, object] = {"status": "open", "limit": limit}
    if mve_filter is not None:
        kwargs["mve_filter"] = mve_filter
    return client.markets.get_markets(**kwargs)


@with_rest_retry
def get_market(client: KalshiSdkClient, ticker: str) -> GetMarketResponse:
    return client.markets.get_market(ticker=ticker)


@with_rest_retry
def get_orderbook(client: KalshiSdkClient, ticker: str) -> GetMarketOrderbookResponse:
    return client.markets.get_market_orderbook(ticker=ticker, depth=10)


def _best_bid_dollars(levels: list[list[str]] | None) -> float | None:
    if not levels:
        return None
    best = 0.0
    for row in levels:
        if len(row) >= 1:
            best = max(best, float(row[0]))
    return best if best > 0 else None


def best_yes_bid_cents(orderbook: GetMarketOrderbookResponse) -> int | None:
    """Best bid to buy YES (highest price in dollars → cents). None if no YES bids."""
    ob = orderbook.orderbook_fp
    if ob is None:
        return None
    b = _best_bid_dollars(list(ob.yes_dollars or []))
    if b is None or b <= 0:
        return None
    return int(round(b * 100))


def best_no_bid_cents(orderbook: GetMarketOrderbookResponse) -> int | None:
    """Best bid on the NO side (dollars → cents)."""
    ob = orderbook.orderbook_fp
    if ob is None:
        return None
    b = _best_bid_dollars(list(ob.no_dollars or []))
    if b is None or b <= 0:
        return None
    return int(round(b * 100))


def summarize_market_row(m: Any) -> MarketSummary:
    """Narrow SDK model to a stable dataclass for CLI/tests."""
    return MarketSummary(
        ticker=m.ticker,
        title=getattr(m, "title", "") or "",
        status=getattr(m, "status", None),
        volume=getattr(m, "volume", None),
    )
