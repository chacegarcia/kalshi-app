"""Strategy interface and a research sample (threshold + spread + probability gap).

**Share model:** Each Kalshi YES **contract** is treated as one **share** of a $1 binary (see
``kalshi_bot.trading_model``). Signals react to **implied YES price** (bid/ask in dollars 0–1) like
a probability “stock”; filters (spread, gap from 50%, max ask) anticipate **price** and liquidity
quality, not a different asset class.

**Plug in your research here:** implement `Strategy` for live WebSocket use, or
`signal_from_bar` / a custom factory for `backtest.run_rule_backtest`.

This repository makes **no claim of profitability**; the sample rule is for wiring tests only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings
from kalshi_bot.edge_math import (
    implied_no_ask_dollars,
    min_edge_threshold_for_mid,
    net_edge_buy_no_long,
    net_edge_buy_yes_long,
)
from kalshi_bot.market_data import fetch_event_markets_sorted_by_yes_score, get_market_entry_timing_and_event
from kalshi_bot.portfolio import PortfolioSnapshot, count_long_yes_positions_matching_substring


def skip_buy_yes_longshot(settings: Settings, yes_ask_cents: int) -> bool:
    """Return True to skip buy-YES when implied YES ask is below the effective floor.

    Kalshi's **chance** (%%) for the YES side matches the implied ask in cents on the $1 contract (e.g. 45%% ≈ 45¢).
    Uses ``TRADE_ENTRY_MIN_YES_ASK_CENTS`` / ``TRADE_ENTRY_MIN_YES_CHANCE_PCT`` and, if set, the minimum implied by
    ``TRADE_ENTRY_MAX_AMERICAN_ODDS_YES`` (e.g. +200 → ~34¢). If the latter is 0, that gate is off unless min cents is set.
    """
    need = settings.trade_entry_effective_min_yes_ask_cents
    if need <= 0:
        return False
    return yes_ask_cents < need


def implied_no_ask_cents_from_yes_bid(yes_bid_cents: int) -> int:
    """Implied lift NO (¢) from best YES bid: 1 − yes_bid on the \$1 face."""
    d = implied_no_ask_dollars(yes_bid_cents / 100.0)
    return int(max(1, min(99, round(d * 100.0))))


def choose_entry_side_and_ask_cents(
    settings: Settings,
    *,
    yes_ask_cents: int,
    yes_bid_cents: int,
    no_bid_cents: int,
) -> tuple[Literal["yes", "no"], int]:
    """Pick YES vs NO by comparing both legs when enabled; else always YES at ``yes_ask_cents``.

    Each leg is scored as ``implied_ask_cents − penalty × spread_cents`` so the market favorite is preferred
    but wide bid–ask books are discounted (better “opportunity” on the tighter line when asks are close).
    """
    if not settings.trade_entry_prefer_higher_odds_side_enabled:
        return "yes", yes_ask_cents
    no_ask_c = implied_no_ask_cents_from_yes_bid(yes_bid_cents)
    pen = float(settings.trade_entry_side_choice_spread_penalty)
    yes_spread = max(0, yes_ask_cents - yes_bid_cents)
    no_spread = max(0, no_ask_c - no_bid_cents)
    score_yes = float(yes_ask_cents) - pen * float(yes_spread)
    score_no = float(no_ask_c) - pen * float(no_spread)
    if score_no > score_yes:
        return "no", no_ask_c
    return "yes", yes_ask_cents


def should_skip_buy_ticker_substrings(settings: Settings, ticker: str) -> bool:
    """True when ``ticker`` contains any token from ``TRADE_ENTRY_SKIP_TICKER_SUBSTRINGS`` (comma-separated, case-insensitive)."""
    for tok in settings.trade_entry_skip_substring_tokens:
        if tok in (ticker or "").upper():
            return True
    return False


def should_skip_buy_due_to_long_yes_cap(
    settings: Settings,
    *,
    ticker: str,
    snap: PortfolioSnapshot,
) -> bool:
    """True when the candidate ticker matches the cap substring and distinct long-YES positions in that family already >= max."""
    m = settings.trade_entry_cap_long_yes_max
    if m <= 0:
        return False
    sub = (settings.trade_entry_cap_long_yes_substring or "").strip()
    if not sub:
        return False
    if sub.upper() not in (ticker or "").upper():
        return False
    n = count_long_yes_positions_matching_substring(snap, sub)
    return n >= m


def should_skip_buy_resolution_too_far(
    settings: Settings,
    *,
    seconds_until_resolution: float | None,
) -> bool:
    """Skip buys when the market resolves later than ``TRADE_ENTRY_MAX_SECONDS_UNTIL_RESOLUTION`` (0 = off)."""
    cap = float(getattr(settings, "trade_entry_max_seconds_until_resolution", 0.0) or 0.0)
    if cap <= 0.0:
        return False
    if seconds_until_resolution is None:
        return False
    return float(seconds_until_resolution) > cap


def should_skip_buy_theta_decay(
    settings: Settings,
    *,
    yes_ask_cents: int,
    seconds_until_close: float | None,
) -> bool:
    """Skip long-shot YES buys when resolution is soon (theta): implied ask in configured band and time-to-close short."""
    if not settings.trade_entry_theta_decay_enabled:
        return False
    if seconds_until_close is None:
        return False
    lo = settings.trade_entry_theta_min_yes_ask_cents
    hi = settings.trade_entry_theta_max_yes_ask_cents
    if not (lo <= yes_ask_cents <= hi):
        return False
    return float(seconds_until_close) <= float(settings.trade_entry_theta_seconds_to_close_max)


def ensure_event_markets_sorted(
    client: KalshiSdkClient,
    event_ticker: str,
    cache: dict[str, list[tuple[str, float]] | None],
) -> list[tuple[str, float]] | None:
    """Cached list of (ticker, REST score) for an event, best first. ``None`` = fetch failed."""
    if event_ticker in cache:
        return cache[event_ticker]
    rows = fetch_event_markets_sorted_by_yes_score(client, event_ticker)
    cache[event_ticker] = rows
    return rows


def ensure_event_top_yes_set(
    client: KalshiSdkClient,
    event_ticker: str,
    top_n: int,
    cache: dict[str, list[tuple[str, float]] | None],
) -> frozenset[str] | None:
    """Cached top-N market tickers in an event by REST implied YES."""
    rows = ensure_event_markets_sorted(client, event_ticker, cache)
    if rows is None:
        return None
    return frozenset(t for t, _ in rows[:top_n])


def should_skip_buy_not_in_event_top_n(
    settings: Settings,
    *,
    ticker: str,
    top_set: frozenset[str] | None,
) -> bool:
    """When enabled + substring matches: skip if this ticker is not in the event's top-N by implied YES."""
    if settings.trade_entry_event_top_n <= 0:
        return False
    sub = (settings.trade_entry_event_top_n_substring or "").strip().upper()
    if not sub or sub not in (ticker or "").upper():
        return False
    if top_set is None:
        return False
    if not top_set:
        return False
    return ticker not in top_set


def entry_filter_timing_and_event(
    settings: Settings,
    client: KalshiSdkClient,
    ticker: str,
    yes_ask_cents: int,
    event_data_cache: dict[str, list[tuple[str, float]] | None],
) -> tuple[bool, str]:
    """Binary vs multi-choice intelligence, theta-decay gate, optional legacy event top-N substring."""
    sec_until: float | None = None
    ev_t: str | None = None
    need_timing = False
    if settings.trade_entry_market_intelligence_enabled:
        need_timing = True
    if settings.trade_entry_theta_decay_enabled:
        lo = settings.trade_entry_theta_min_yes_ask_cents
        hi = settings.trade_entry_theta_max_yes_ask_cents
        if lo <= yes_ask_cents <= hi:
            need_timing = True
    sub_ev = (settings.trade_entry_event_top_n_substring or "").strip().upper()
    if settings.trade_entry_event_top_n > 0 and sub_ev and sub_ev in ticker.upper():
        need_timing = True
    # Max resolution horizon (TRADE_ENTRY_MAX_SECONDS_UNTIL_RESOLUTION) needs real seconds-to-close; it is *not*
    # implied by TRADE_ENTRY_THETA_* (theta only applies to the long-shot 1–10¢ band).
    resolution_cap_on = float(getattr(settings, "trade_entry_max_seconds_until_resolution", 0.0) or 0.0) > 0.0
    if resolution_cap_on:
        need_timing = True
    if need_timing:
        sec_until, ev_t = get_market_entry_timing_and_event(client, ticker)
    else:
        sec_until, ev_t = None, None

    if should_skip_buy_resolution_too_far(settings, seconds_until_resolution=sec_until):
        return True, "resolution_too_far"

    if settings.trade_entry_market_intelligence_enabled and ev_t:
        rows = ensure_event_markets_sorted(client, ev_t, event_data_cache)
        if rows is not None and len(rows) >= 2:
            rank = next((i for i, (tk, _) in enumerate(rows) if tk == ticker), None)
            if rank is None:
                return True, "multi_choice_ticker_not_in_event"
            if rank >= settings.trade_entry_multi_choice_top_n:
                return True, "multi_choice_not_top_n"
            if yes_ask_cents < settings.trade_entry_multi_choice_min_yes_ask_cents:
                return True, "multi_choice_below_min_chance"

    if settings.trade_entry_theta_decay_enabled:
        lo = settings.trade_entry_theta_min_yes_ask_cents
        hi = settings.trade_entry_theta_max_yes_ask_cents
        if lo <= yes_ask_cents <= hi:
            if should_skip_buy_theta_decay(
                settings,
                yes_ask_cents=yes_ask_cents,
                seconds_until_close=sec_until,
            ):
                return True, "theta_decay_longshot"
    if settings.trade_entry_event_top_n > 0 and sub_ev and sub_ev in ticker.upper():
        if not ev_t:
            return False, ""
        top_set = ensure_event_top_yes_set(client, ev_t, settings.trade_entry_event_top_n, event_data_cache)
        if should_skip_buy_not_in_event_top_n(settings, ticker=ticker, top_set=top_set):
            return True, "not_in_event_top_yes"
    return False, ""


@dataclass
class TradeIntent:
    """Desired order (execution layer decides dry-run vs live after risk checks)."""

    ticker: str
    side: str  # "yes" | "no"
    action: str  # "buy" | "sell"
    count: int
    yes_price_cents: int
    time_in_force: str = "good_till_canceled"
    # If true: allow buy-YES to reach TRADE_DOUBLE_DOWN_MAX_POSITION_CONTRACTS (add-on to existing long).
    double_down: bool = False


def signed_position_delta(intent: TradeIntent) -> float:
    """Net YES contracts added (Kalshi convention: long YES > 0, long NO < 0)."""
    c = float(intent.count)
    if intent.side == "yes" and intent.action == "buy":
        return c
    if intent.side == "yes" and intent.action == "sell":
        return -c
    if intent.side == "no" and intent.action == "buy":
        return -c
    if intent.side == "no" and intent.action == "sell":
        return c
    return 0.0


def projected_abs_position_after(signed: float, intent: TradeIntent) -> float:
    """Absolute net position after the order (for per-market contract cap)."""
    return abs(signed + signed_position_delta(intent))


class Strategy(Protocol):
    """Implement for live WebSocket-driven trading."""

    def on_ticker_message(self, message: dict[str, Any]) -> TradeIntent | None:
        """Handle one WebSocket payload (decoded JSON)."""


def _parse_dollar_field(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val))
    except ValueError:
        return None


def signal_from_bar(
    *,
    ticker: str,
    yes_bid_dollars: float,
    yes_ask_dollars: float,
    max_yes_ask_dollars: float,
    min_spread_dollars: float,
    probability_gap: float,
    order_count: int,
    limit_price_cents: int,
    max_spread_dollars: float | None = None,
    entry_min_yes_ask_cents: int = 0,
) -> TradeIntent | None:
    """Shared rule logic for both WebSocket tickers and backtest `PriceRecord` rows.

    **Replace this function** with your own signal logic to experiment with rules.
    """
    spread = max(0.0, yes_ask_dollars - yes_bid_dollars)
    if spread < min_spread_dollars:
        return None
    if max_spread_dollars is not None and spread > max_spread_dollars:
        return None

    yes_ask_c = int(max(1, min(99, round(yes_ask_dollars * 100.0))))
    if entry_min_yes_ask_cents > 0 and yes_ask_c < entry_min_yes_ask_cents:
        return None

    mid = (yes_bid_dollars + yes_ask_dollars) / 2.0
    if abs(mid - 0.5) < probability_gap:
        return None

    if yes_ask_dollars > max_yes_ask_dollars:
        return None

    return TradeIntent(
        ticker=ticker,
        side="yes",
        action="buy",
        count=order_count,
        yes_price_cents=limit_price_cents,
    )


def signal_edge_buy_yes_from_ticker(
    *,
    ticker: str,
    yes_bid_dollars: float,
    yes_ask_dollars: float,
    settings: Settings,
) -> TradeIntent | None:
    """Buy YES only if ``fair_yes − ask − taker fee`` clears a mid-aware minimum edge (fee-aware ideology)."""
    spread = max(0.0, yes_ask_dollars - yes_bid_dollars)
    if spread < settings.strategy_min_spread_dollars:
        return None
    if settings.trade_max_entry_spread_dollars is not None and spread > settings.trade_max_entry_spread_dollars:
        return None

    yes_ask_c = int(max(1, min(99, round(yes_ask_dollars * 100.0))))
    if skip_buy_yes_longshot(settings, yes_ask_c):
        return None

    fair = settings.trade_fair_yes_prob
    if fair is None:
        return None

    mid = (yes_bid_dollars + yes_ask_dollars) / 2.0
    c = settings.strategy_order_count
    edge = net_edge_buy_yes_long(fair_yes=fair, yes_ask_dollars=yes_ask_dollars, contracts=c)
    need = min_edge_threshold_for_mid(
        mid,
        base_min_edge=settings.trade_min_net_edge_after_fees,
        middle_extra=settings.trade_edge_middle_extra_edge,
    )
    if edge < need:
        return None

    if yes_ask_dollars > settings.trade_entry_effective_max_yes_ask_dollars:
        return None

    limit_cents = int(max(1, min(99, round(yes_ask_dollars * 100.0))))
    return TradeIntent(
        ticker=ticker,
        side="yes",
        action="buy",
        count=c,
        yes_price_cents=limit_cents,
    )


def signal_from_bar_buy_no(
    *,
    ticker: str,
    no_bid_dollars: float,
    no_ask_dollars: float,
    max_yes_ask_dollars: float,
    min_spread_dollars: float,
    probability_gap: float,
    order_count: int,
    limit_price_cents: int,
    max_spread_dollars: float | None = None,
    entry_min_yes_ask_cents: int = 0,
) -> TradeIntent | None:
    """Same structural filters as ``signal_from_bar`` but for the NO contract (bid/ask on NO)."""
    spread = max(0.0, no_ask_dollars - no_bid_dollars)
    if spread < min_spread_dollars:
        return None
    if max_spread_dollars is not None and spread > max_spread_dollars:
        return None

    no_ask_c = int(max(1, min(99, round(no_ask_dollars * 100.0))))
    if entry_min_yes_ask_cents > 0 and no_ask_c < entry_min_yes_ask_cents:
        return None

    mid = (no_bid_dollars + no_ask_dollars) / 2.0
    if abs(mid - 0.5) < probability_gap:
        return None

    if no_ask_dollars > max_yes_ask_dollars:
        return None

    return TradeIntent(
        ticker=ticker,
        side="no",
        action="buy",
        count=order_count,
        yes_price_cents=limit_price_cents,
    )


def signal_edge_buy_no_from_ticker(
    *,
    ticker: str,
    no_bid_dollars: float,
    no_ask_dollars: float,
    settings: Settings,
) -> TradeIntent | None:
    """Buy NO only if ``fair_no − ask − taker fee`` clears the same mid-aware edge thresholds as YES."""
    spread = max(0.0, no_ask_dollars - no_bid_dollars)
    if spread < settings.strategy_min_spread_dollars:
        return None
    if settings.trade_max_entry_spread_dollars is not None and spread > settings.trade_max_entry_spread_dollars:
        return None

    no_ask_c = int(max(1, min(99, round(no_ask_dollars * 100.0))))
    if skip_buy_yes_longshot(settings, no_ask_c):
        return None

    fair_yes = settings.trade_fair_yes_prob
    if fair_yes is None:
        return None
    fair_no = 1.0 - float(fair_yes)

    mid = (no_bid_dollars + no_ask_dollars) / 2.0
    c = settings.strategy_order_count
    edge = net_edge_buy_no_long(fair_no=fair_no, no_ask_dollars=no_ask_dollars, contracts=c)
    need = min_edge_threshold_for_mid(
        mid,
        base_min_edge=settings.trade_min_net_edge_after_fees,
        middle_extra=settings.trade_edge_middle_extra_edge,
    )
    if edge < need:
        return None

    if no_ask_dollars > settings.trade_entry_effective_max_yes_ask_dollars:
        return None

    limit_cents = int(max(1, min(99, round(no_ask_dollars * 100.0))))
    return TradeIntent(
        ticker=ticker,
        side="no",
        action="buy",
        count=c,
        yes_price_cents=limit_cents,
    )


@dataclass
class SampleSpreadGapStrategy:
    """Research sample: require min spread + probability gap away from 0.5, cap on YES ask."""

    settings: Settings

    def on_ticker_message(self, message: dict[str, Any]) -> TradeIntent | None:
        if message.get("type") != "ticker":
            return None
        body = message.get("msg") or {}
        ticker = body.get("market_ticker") or body.get("ticker")
        if not ticker or ticker != self.settings.strategy_market_ticker:
            return None

        bid = _parse_dollar_field(body.get("yes_bid_dollars"))
        ask = _parse_dollar_field(body.get("yes_ask_dollars"))
        if bid is None or ask is None:
            return None

        if self.settings.trade_use_edge_strategy and self.settings.trade_fair_yes_prob is not None:
            return signal_edge_buy_yes_from_ticker(
                ticker=ticker,
                yes_bid_dollars=bid,
                yes_ask_dollars=ask,
                settings=self.settings,
            )

        return signal_from_bar(
            ticker=ticker,
            yes_bid_dollars=bid,
            yes_ask_dollars=ask,
            max_yes_ask_dollars=self.settings.trade_entry_effective_max_yes_ask_dollars,
            min_spread_dollars=self.settings.strategy_min_spread_dollars,
            probability_gap=self.settings.strategy_probability_gap,
            order_count=self.settings.strategy_order_count,
            limit_price_cents=self.settings.strategy_limit_price_cents,
            max_spread_dollars=self.settings.trade_max_entry_spread_dollars,
            entry_min_yes_ask_cents=self.settings.trade_entry_effective_min_yes_ask_cents,
        )


# Backward-compatible name
SampleThresholdStrategy = SampleSpreadGapStrategy


def make_bar_strategy_fn(params: dict[str, Any]):
    """Factory for `backtest.run_rule_backtest` / `parameter_sweep`."""

    def _fn(rec: Any) -> TradeIntent | None:
        return signal_from_bar(
            ticker=str(params.get("ticker", rec.ticker)),
            yes_bid_dollars=rec.yes_bid_dollars,
            yes_ask_dollars=rec.yes_ask_dollars,
            max_yes_ask_dollars=float(params["max_yes_ask_dollars"]),
            min_spread_dollars=float(params.get("min_spread_dollars", 0)),
            probability_gap=float(params.get("probability_gap", 0)),
            order_count=int(params.get("order_count", 1)),
            limit_price_cents=int(params["limit_price_cents"]),
            max_spread_dollars=params.get("max_spread_dollars"),
            entry_min_yes_ask_cents=int(params.get("trade_entry_min_yes_ask_cents", 0)),
        )

    return _fn
