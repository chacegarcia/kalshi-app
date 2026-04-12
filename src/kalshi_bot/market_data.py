"""REST helpers for markets and order books."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kalshi_python_sync.models.get_market_orderbook_response import GetMarketOrderbookResponse
from kalshi_python_sync.models.get_market_response import GetMarketResponse
from kalshi_python_sync.models.get_markets_response import GetMarketsResponse

from kalshi_bot.client import KalshiSdkClient, with_rest_retry
from kalshi_bot.edge_math import implied_no_ask_dollars, implied_yes_ask_dollars  # re-export for convenience


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


@dataclass(frozen=True)
class TakerTapeLean:
    """Aggregate taker flow on one market: YES-taker vs NO-taker notional (anonymous public prints)."""

    trade_count: int
    taker_yes_notional_usd: float
    taker_no_notional_usd: float

    @property
    def total_notional_usd(self) -> float:
        return self.taker_yes_notional_usd + self.taker_no_notional_usd

    @property
    def yes_share(self) -> float | None:
        """Fraction of taker $ on YES side; None if no parsed tape."""
        tot = self.total_notional_usd
        if tot <= 0.0:
            return None
        return self.taker_yes_notional_usd / tot

    def lean_label(self) -> str:
        """Short text for dashboards: which side anonymous takers leaned recently."""
        if self.trade_count == 0:
            return "no_tape"
        sp = self.yes_share
        if sp is None:
            return "unknown"
        if sp >= 0.6:
            return "yes-heavy"
        if sp <= 0.4:
            return "no-heavy"
        return "mixed"


def summarize_taker_tape_lean(trades: list[Any]) -> TakerTapeLean:
    """Split public prints by ``taker_side`` (Kalshi: taker lifted YES vs NO)."""
    yes_usd = 0.0
    no_usd = 0.0
    n = 0
    for t in trades:
        side = getattr(t, "taker_side", None)
        if side is None:
            continue
        s = str(side).strip().lower()
        try:
            cnt = float(str(getattr(t, "count_fp", "0")))
        except (TypeError, ValueError):
            continue
        if cnt <= 0:
            continue
        if s == "yes":
            try:
                yp = float(str(getattr(t, "yes_price_dollars", "0")))
            except (TypeError, ValueError):
                continue
            yes_usd += cnt * yp
            n += 1
        elif s == "no":
            try:
                np_ = float(str(getattr(t, "no_price_dollars", "0")))
            except (TypeError, ValueError):
                continue
            no_usd += cnt * np_
            n += 1
    return TakerTapeLean(
        trade_count=n,
        taker_yes_notional_usd=yes_usd,
        taker_no_notional_usd=no_usd,
    )


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


@with_rest_retry
def fetch_public_trades_for_ticker(
    client: KalshiSdkClient,
    ticker: str,
    *,
    max_trades: int,
    page_limit: int = 1000,
) -> list[Any]:
    """Recent public trades for a single market (taker_side + prices). Used for position / flow lean."""
    out: list[Any] = []
    cursor: str | None = None
    page_limit = max(1, min(1000, page_limit))
    while len(out) < max_trades:
        take = min(page_limit, max_trades - len(out))
        resp = client.markets.get_trades(limit=take, cursor=cursor, ticker=ticker)
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


def lift_yes_ask_cents_from_orderbook(orderbook: GetMarketOrderbookResponse) -> int | None:
    """Lift YES ask (¢) from complement of best NO bid; ``None`` if the book cannot be used for trading."""
    _yb, nb = yes_bid_and_no_bid_cents_for_trading(orderbook)
    if nb is None:
        return None
    ya_d = implied_yes_ask_dollars(nb / 100.0)
    return int(max(1, min(99, round(ya_d * 100.0))))


def summarize_market_row(m: Any) -> MarketSummary:
    """Narrow SDK model to a stable dataclass for CLI/tests."""
    return MarketSummary(
        ticker=m.ticker,
        title=getattr(m, "title", "") or "",
        status=getattr(m, "status", None),
        volume=getattr(m, "volume", None),
    )


def seconds_until_resolution(m: Any) -> float | None:
    """Seconds until the earliest of close / expected / latest expiration (vs UTC now). None if unknown."""
    now = datetime.now(UTC)
    best: float | None = None
    for attr in ("close_time", "expected_expiration_time", "latest_expiration_time"):
        t = getattr(m, attr, None)
        if t is None:
            continue
        if isinstance(t, datetime):
            dt = t if t.tzinfo else t.replace(tzinfo=UTC)
            sec = (dt - now).total_seconds()
            if best is None or sec < best:
                best = sec
    return best


def get_market_entry_timing_and_event(
    client: KalshiSdkClient, ticker: str
) -> tuple[float | None, str | None]:
    """REST: seconds until resolution (soonest deadline) and ``event_ticker`` for grouping."""
    try:
        row = get_market(client, ticker=ticker)
        m = getattr(row, "market", None)
        if m is None:
            return None, None
        ev = getattr(m, "event_ticker", None)
        return seconds_until_resolution(m), str(ev) if ev else None
    except Exception:
        return None, None


def _event_market_yes_score(m: Any) -> float:
    """Rank markets within an event: prefer REST implied YES ask, then last, then volume."""
    ya = getattr(m, "yes_ask_dollars", None)
    if ya is not None:
        try:
            return float(str(ya))
        except (TypeError, ValueError):
            pass
    lp = getattr(m, "last_price_dollars", None)
    if lp is not None:
        try:
            return float(str(lp))
        except (TypeError, ValueError):
            pass
    vol = getattr(m, "volume_fp", None)
    if vol is None:
        vol = getattr(m, "volume", None)
    if vol is not None:
        try:
            return float(str(vol)) * 1e-9
        except (TypeError, ValueError):
            pass
    return -1.0


@with_rest_retry
def fetch_event_markets_sorted_by_yes_score(
    client: KalshiSdkClient,
    event_ticker: str,
    *,
    mve_filter: str | None = "exclude",
) -> list[tuple[str, float]] | None:
    """All open markets in ``event_ticker``, sorted by REST implied YES score (high first). None on API failure."""
    if not event_ticker:
        return []
    rows: list[tuple[str, float]] = []
    cursor: str | None = None
    try:
        while True:
            resp = client.markets.get_markets(
                status="open",
                event_ticker=event_ticker,
                limit=1000,
                cursor=cursor,
                mve_filter=mve_filter,
            )
            markets = list(getattr(resp, "markets", []) or [])
            for m in markets:
                tk = getattr(m, "ticker", None)
                if not tk:
                    continue
                rows.append((str(tk), _event_market_yes_score(m)))
            cursor = getattr(resp, "cursor", None)
            if not cursor or not markets:
                break
    except Exception:
        return None
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows


@with_rest_retry
def fetch_event_top_yes_tickers(
    client: KalshiSdkClient,
    event_ticker: str,
    top_n: int,
    *,
    mve_filter: str | None = "exclude",
) -> list[str] | None:
    """Open markets in ``event_ticker`` ranked by implied YES (REST fields). None on API failure; may be empty."""
    if not event_ticker or top_n < 1:
        return []
    rows = fetch_event_markets_sorted_by_yes_score(client, event_ticker, mve_filter=mve_filter)
    if rows is None:
        return None
    return [t for t, _ in rows[:top_n]]


_MARKET_DISPLAY_CACHE: dict[str, tuple[str, str]] = {}


def _market_title_and_category(client: KalshiSdkClient, ticker: str) -> tuple[str, str]:
    """Return ``(title, category)``. Category comes from the event (e.g. Sports, Crypto); may be empty."""
    if ticker in _MARKET_DISPLAY_CACHE:
        return _MARKET_DISPLAY_CACHE[ticker]
    title = ""
    category = ""
    try:
        row = get_market(client, ticker=ticker)
        m = getattr(row, "market", None)
        if m is None:
            _MARKET_DISPLAY_CACHE[ticker] = ("", "")
            return "", ""
        title = (summarize_market_row(m).title or "").strip()
        et = getattr(m, "event_ticker", None)
        if et:
            try:
                evr = client.events.get_event(event_ticker=str(et))
                ev = getattr(evr, "event", None)
                if ev is not None:
                    c = getattr(ev, "category", None)
                    if c is not None:
                        category = str(c).strip()
            except Exception:
                pass
    except Exception:
        pass
    _MARKET_DISPLAY_CACHE[ticker] = (title, category)
    return title, category


def market_title_for_ticker(client: KalshiSdkClient, ticker: str) -> str:
    """Kalshi market ``title`` (what the contract is about) for logs and the HTML monitor.

    Cached per ticker for the process. Returns ``\"\"`` if the lookup fails.
    """
    t, _c = _market_title_and_category(client, ticker)
    return t


def market_category_for_ticker(client: KalshiSdkClient, ticker: str) -> str:
    """Event ``category`` from Kalshi (e.g. sports-oriented labels); may be empty if unknown."""
    _t, c = _market_title_and_category(client, ticker)
    return c
