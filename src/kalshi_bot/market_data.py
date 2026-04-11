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


@dataclass(frozen=True)
class TapeUniverseEntry:
    """One market in the tape-ranked universe for ``llm-trade --tape``.

    ``flow_usd_approx`` sums ``count × yes_price`` over recent **anonymous** public prints (see
    ``rank_tickers_by_public_flow``). ``rank`` is 1-based among markets that pass min-flow / volume filters.
    """

    ticker: str
    title: str
    flow_usd_approx: float
    public_trade_count: int
    rank: int


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
) -> tuple[list[TapeUniverseEntry], int]:
    """Tape-ranked markets (flow + titles) for ``llm-trade --tape``. Returns entries and raw trades fetched."""
    raw = fetch_public_trades(client, max_trades=max_trades_fetch)
    ranked = rank_tickers_by_public_flow(raw)
    out: list[TapeUniverseEntry] = []
    for ticker, flow_usd, n_trades in ranked:
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
        out.append(
            TapeUniverseEntry(
                ticker=ticker,
                title=title,
                flow_usd_approx=flow_usd,
                public_trade_count=n_trades,
                rank=len(out) + 1,
            )
        )
    return out, len(raw)


@with_rest_retry
def _get_markets_page(
    client: KalshiSdkClient,
    *,
    cursor: str | None,
    limit: int,
    mve_filter: str | None,
) -> GetMarketsResponse:
    """One page of open markets (for pagination loops)."""
    kwargs: dict[str, object] = {"status": "open", "limit": max(1, min(1000, limit))}
    if cursor is not None:
        kwargs["cursor"] = cursor
    if mve_filter is not None:
        kwargs["mve_filter"] = mve_filter
    return client.markets.get_markets(**kwargs)


def fetch_open_markets_by_ticker_prefix(
    client: KalshiSdkClient,
    *,
    prefix: str,
    max_results: int,
    max_api_pages: int = 40,
    page_limit: int = 200,
    mve_filter: str | None = "exclude",
) -> list[MarketSummary]:
    """Paginate ``get_markets`` and collect **open** markets whose ``ticker`` starts with ``prefix``.

    Results are de-duplicated by ticker, sorted by ``volume`` descending (unknown volume last),
    then truncated to ``max_results``. Bitcoin (and other) series roll frequently; use this instead
    of pinning a single contract ticker.
    """
    if not prefix:
        return []
    want = max(1, max_results)
    seen: dict[str, MarketSummary] = {}
    cursor: str | None = None
    pages = 0
    while len(seen) < want and pages < max(1, max_api_pages):
        resp = _get_markets_page(
            client, cursor=cursor, limit=min(page_limit, 1000), mve_filter=mve_filter
        )
        markets = list(getattr(resp, "markets", []) or [])
        for m in markets:
            tk = getattr(m, "ticker", None) or ""
            if not tk.startswith(prefix):
                continue
            if tk not in seen:
                seen[tk] = summarize_market_row(m)
        cursor = getattr(resp, "cursor", None)
        pages += 1
        if not cursor or not markets:
            break

    rows = list(seen.values())

    def _vol_key(s: MarketSummary) -> int:
        return int(s.volume) if s.volume is not None else -1

    rows.sort(key=_vol_key, reverse=True)
    return rows[:want]


def fetch_open_markets_unique_up_to(
    client: KalshiSdkClient,
    *,
    target_count: int,
    mve_filter: str | None = "exclude",
    max_pages: int = 40,
    page_limit: int = 200,
    leading_pages_to_skip: int = 0,
) -> list[MarketSummary]:
    """Paginate ``get_markets`` until we have ``target_count`` **distinct** tickers (or run out of pages).

    A single REST page often repeats ordering or returns fewer rows than ``limit``; ``llm-trade`` uses
    this instead of one ``list_open_markets`` call so each run sees diverse markets.

    ``leading_pages_to_skip`` advances the cursor that many pages **without** recording tickers so each
    run can start in a different part of the catalog (combine with shuffle for fresher mixes).
    """
    want = max(1, min(2000, target_count))
    seen: dict[str, MarketSummary] = {}
    cursor: str | None = None
    skip_left = max(0, int(leading_pages_to_skip))
    while skip_left > 0:
        resp = _get_markets_page(
            client, cursor=cursor, limit=min(page_limit, 1000), mve_filter=mve_filter
        )
        markets = list(getattr(resp, "markets", []) or [])
        cursor = getattr(resp, "cursor", None)
        skip_left -= 1
        if not markets or not cursor:
            break
    pages = 0
    while len(seen) < want and pages < max(1, max_pages):
        resp = _get_markets_page(
            client, cursor=cursor, limit=min(page_limit, 1000), mve_filter=mve_filter
        )
        markets = list(getattr(resp, "markets", []) or [])
        for m in markets:
            s = summarize_market_row(m)
            if s.ticker not in seen:
                seen[s.ticker] = s
        cursor = getattr(resp, "cursor", None)
        pages += 1
        if not markets:
            break
        if not cursor:
            break
    return list(seen.values())[:want]


def build_llm_trade_open_universe(
    client: KalshiSdkClient,
    *,
    target_count: int,
    max_pages: int,
    page_limit: int = 200,
    mve_filter: str | None = "exclude",
    leading_pages_to_skip: int = 0,
    bitcoin_prefix: str | None = None,
    bitcoin_max_markets: int = 0,
) -> list[MarketSummary]:
    """Open markets for ``llm-trade``: optional **Bitcoin-first** block (by prefix), then paginated unique tickers.

    Bitcoin rows are volume-sorted from ``fetch_open_markets_by_ticker_prefix``; the rest fills from
    ``fetch_open_markets_unique_up_to`` with duplicates removed so the LLM sees BTC focus plus a broad,
    de-duplicated mix each run.
    """
    want = max(1, min(2000, target_count))
    prefix = (bitcoin_prefix or "").strip()
    quota = max(0, min(want, int(bitcoin_max_markets)))
    btc_rows: list[MarketSummary] = []
    if prefix and quota > 0:
        btc_rows = fetch_open_markets_by_ticker_prefix(
            client,
            prefix=prefix,
            max_results=quota,
            max_api_pages=max_pages,
            page_limit=page_limit,
            mve_filter=mve_filter,
        )
    btc_set = {s.ticker for s in btc_rows}
    need_rest = max(0, want - len(btc_rows))
    if need_rest == 0:
        return btc_rows[:want]
    # Request extra rows from the general walk in case of overlap with BTC list.
    rest = fetch_open_markets_unique_up_to(
        client,
        target_count=need_rest + min(200, len(btc_set) + 50),
        mve_filter=mve_filter,
        max_pages=max_pages,
        page_limit=page_limit,
        leading_pages_to_skip=leading_pages_to_skip,
    )
    merged = list(btc_rows) + [s for s in rest if s.ticker not in btc_set]
    return merged[:want]


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

    For scans that need many **distinct** tickers, prefer `fetch_open_markets_unique_up_to`.
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


def yes_bid_and_no_bid_cents_for_trading(
    orderbook: GetMarketOrderbookResponse,
    *,
    synthetic_yes_bid_if_empty: int = 1,
) -> tuple[int | None, int | None]:
    """Best YES and NO bids. If the YES book is empty but NO has bids, use a synthetic YES bid (default 1¢).

    Thin or one-sided books (common on short-dated BTC ladders) often have no resting YES bids while NO bids exist;
    we still need a YES bid for spread/edge math, so we assume the minimum tick on the YES side.
    """
    yb = best_yes_bid_cents(orderbook)
    nb = best_no_bid_cents(orderbook)
    if nb is None:
        return None, None
    if yb is None and synthetic_yes_bid_if_empty > 0:
        yb = max(1, min(99, int(synthetic_yes_bid_if_empty)))
    return yb, nb


def summarize_market_row(m: Any) -> MarketSummary:
    """Narrow SDK model to a stable dataclass for CLI/tests."""
    return MarketSummary(
        ticker=m.ticker,
        title=getattr(m, "title", "") or "",
        status=getattr(m, "status", None),
        volume=getattr(m, "volume", None),
    )
