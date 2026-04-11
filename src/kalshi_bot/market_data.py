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
def list_open_markets(client: KalshiSdkClient, *, limit: int = 50) -> GetMarketsResponse:
    return client.markets.get_markets(status="open", limit=limit)


@with_rest_retry
def get_market(client: KalshiSdkClient, ticker: str) -> GetMarketResponse:
    return client.markets.get_market(ticker=ticker)


@with_rest_retry
def get_orderbook(client: KalshiSdkClient, ticker: str) -> GetMarketOrderbookResponse:
    return client.markets.get_market_orderbook(ticker=ticker, depth=10)


def summarize_market_row(m: Any) -> MarketSummary:
    """Narrow SDK model to a stable dataclass for CLI/tests."""
    return MarketSummary(
        ticker=m.ticker,
        title=getattr(m, "title", "") or "",
        status=getattr(m, "status", None),
        volume=getattr(m, "volume", None),
    )
