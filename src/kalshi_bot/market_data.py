"""REST helpers for markets and order books."""

from __future__ import annotations

import time
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


def build_tape_universe_for_llm(
    client: KalshiSdkClient,
    *,
    max_trades_fetch: int,
    top_markets: int,
    min_flow_usd: float,
    min_market_volume: int | None,
) -> tuple[list[tuple[str, str]], int]:
    """Tape-ranked tickers with titles for ``llm-trade --tape``. Returns ``(ticker, title)`` rows and trades fetched."""
    raw = fetch_public_trades(client, max_trades=max_trades_fetch)
    ranked = rank_tickers_by_public_flow(raw)
    out: list[tuple[str, str]] = []
    for ticker, flow_usd, _n in ranked:
        if len(out) >= top_markets:
            break
        if min_flow_usd > 0.0 and flow_usd < min_flow_usd:
            continue
        title = ticker
        try:
            mrow = get_market(client, ticker=ticker)
            m = getattr(mrow, "market", None)
            if m is not None:
                s = summarize_market_row(m)
                title = s.title or ticker
                if min_market_volume is not None:
                    vol = s.volume
                    if vol is None or vol < min_market_volume:
                        continue
        except Exception:
            if min_market_volume is not None:
                continue
        out.append((ticker, title))
    return out, len(raw)


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


def _candle_close_dollars(candle: Any) -> float | None:
    p = getattr(candle, "price", None)
    if p is None:
        return None
    s = getattr(p, "close_dollars", None)
    if s is None:
        return None
    try:
        v = float(str(s))
    except (TypeError, ValueError):
        return None
    return v


@with_rest_retry
def fetch_yes_close_prices(
    client: KalshiSdkClient,
    ticker: str,
    *,
    period_interval_minutes: int,
    lookback_seconds: int,
) -> list[float]:
    """YES trade close prices (dollars 0–1) from market candlesticks, oldest → newest."""
    end_ts = int(time.time())
    start_ts = max(0, end_ts - max(60, lookback_seconds))
    resp = client.markets.batch_get_market_candlesticks(
        market_tickers=ticker,
        start_ts=start_ts,
        end_ts=end_ts,
        period_interval=period_interval_minutes,
        include_latest_before_start=True,
    )
    markets = list(getattr(resp, "markets", []) or [])
    for m in markets:
        if getattr(m, "market_ticker", None) != ticker:
            continue
        sticks = list(getattr(m, "candlesticks", []) or [])
        rows: list[tuple[int, float]] = []
        for c in sticks:
            ts = getattr(c, "end_period_ts", None)
            cl = _candle_close_dollars(c)
            if ts is None or cl is None:
                continue
            rows.append((int(ts), cl))
        rows.sort(key=lambda x: x[0])
        return [r[1] for r in rows]
    return []


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
