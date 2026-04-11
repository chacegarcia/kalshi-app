"""Strategy interface and a research sample (threshold + spread + probability gap).

**Plug in your research here:** implement `Strategy` for live WebSocket use, or
`signal_from_bar` / a custom factory for `backtest.run_rule_backtest`.

This repository makes **no claim of profitability**; the sample rule is for wiring tests only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from kalshi_bot.config import Settings
from kalshi_bot.edge_math import min_edge_threshold_for_mid, net_edge_buy_yes_long


@dataclass
class TradeIntent:
    """Desired order (execution layer decides dry-run vs live after risk checks)."""

    ticker: str
    side: str  # "yes" | "no"
    action: str  # "buy" | "sell"
    count: int
    yes_price_cents: int
    time_in_force: str = "good_till_canceled"


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
) -> TradeIntent | None:
    """Shared rule logic for both WebSocket tickers and backtest `PriceRecord` rows.

    **Replace this function** with your own signal logic to experiment with rules.
    """
    spread = max(0.0, yes_ask_dollars - yes_bid_dollars)
    if spread < min_spread_dollars:
        return None
    if max_spread_dollars is not None and spread > max_spread_dollars:
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

    if yes_ask_dollars > settings.strategy_max_yes_ask_dollars:
        return None

    limit_cents = int(max(1, min(99, round(yes_ask_dollars * 100.0))))
    return TradeIntent(
        ticker=ticker,
        side="yes",
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
            max_yes_ask_dollars=self.settings.strategy_max_yes_ask_dollars,
            min_spread_dollars=self.settings.strategy_min_spread_dollars,
            probability_gap=self.settings.strategy_probability_gap,
            order_count=self.settings.strategy_order_count,
            limit_price_cents=self.settings.strategy_limit_price_cents,
            max_spread_dollars=self.settings.trade_max_entry_spread_dollars,
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
        )

    return _fn
