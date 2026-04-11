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
def fetch_public_trades(
    client: KalshiSdkClient,
    *,
    max_trades: int,
    page_limit: int = 1000,
) -> list[Any]:
    """Recent public trades (all markets). No per-user identity — Kalshi does not expose counterparties.

    Used to rank **tickers** by flow; not copy-trading of specific accounts.
    """
    out: list[Any] = []
    cursor: str | None = None
    page_limit = max(1, min(1000, page_limit))
    while len(out) < max_trades:
        take = min(page_limit, max_trades - len(out))
        resp = client.markets.get_trades(limit=take, cursor=cursor)
        batch = list(getattr(resp, "trades", []) or [])
        out.extend(batch)
        cursor = getattr(resp, "cursor", None)
        if not batch or not cursor:
            break
    return out[:max_trades]


def rank_tickers_by_public_flow(trades: list[Any]) -> list[tuple[str, float, int]]:
    """Return ``(ticker, approx_usd_flow, trade_count)`` sorted by flow descending."""
    from collections import defaultdict

    agg: dict[str, list[float | int]] = defaultdict(lambda: [0.0, 0])
    for t in trades:
        ticker = getattr(t, "ticker", None)
        if not ticker:
            continue
        try:
            cnt = float(str(getattr(t, "count_fp", "0")))
            yp = float(str(getattr(t, "yes_price_dollars", "0")))
        except (TypeError, ValueError):
            continue
        row = agg[ticker]
        row[0] = float(row[0]) + cnt * yp
        row[1] = int(row[1]) + 1
    ranked = sorted(agg.items(), key=lambda x: float(x[1][0]), reverse=True)
    return [(tk, float(v[0]), int(v[1])) for tk, v in ranked]


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
