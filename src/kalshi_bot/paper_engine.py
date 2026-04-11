"""Simulated fills and paper portfolio for research (configurable assumptions).

This does **not** replicate Kalshi's matching engine. It is a transparent toy model:
you choose fill probability, partial fills, fees, and slippage to stress-test ideas.

**Plug-in point:** swap `match_limit_order` with your own microstructure model
or call an external simulator.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from kalshi_bot.metrics import TradeOutcome
from kalshi_bot.strategy import TradeIntent


@dataclass
class PaperFillConfig:
    """Assumptions for hypothetical execution."""

    # Probability that a resting limit is touched within the bar when price trades through
    fill_probability_if_crossed: float = 0.85
    # Fraction of requested contracts filled when a fill occurs (1.0 = full)
    partial_fill_fraction: float = 1.0
    # Per-contract fee in cents (stress-test; set 0 to ignore)
    fee_cents_per_contract: float = 0.0
    # Additional adverse cents per contract (slippage)
    slippage_cents_per_contract: float = 0.0
    # Use deterministic fills when rng is None (seed with PaperPortfolio seed)
    deterministic: bool = False


@dataclass
class MarketSnapshot:
    """Minimal top-of-book snapshot for simulation (your recorded data should map here)."""

    yes_bid_dollars: float
    yes_ask_dollars: float

    @property
    def mid_dollars(self) -> float:
        return (self.yes_bid_dollars + self.yes_ask_dollars) / 2.0

    @property
    def spread_dollars(self) -> float:
        return max(0.0, self.yes_ask_dollars - self.yes_bid_dollars)


@dataclass
class PaperPortfolio:
    """Cash + position in cents; YES contracts approximated at entry price."""

    cash_cents: float = 0.0
    position_contracts: float = 0.0
    avg_entry_cents: float = 0.0
    equity_history: list[float] = field(default_factory=list)

    def mark_equity(self, mid_cents: float) -> None:
        """Mark-to-market equity in cents."""
        pos = self.position_contracts
        val = self.cash_cents + pos * mid_cents
        self.equity_history.append(val)

    def apply_buy_yes(
        self,
        *,
        contracts: float,
        price_cents: float,
        fee_slippage_cents: float,
    ) -> None:
        cost = contracts * price_cents + fee_slippage_cents
        self.cash_cents -= cost
        if self.position_contracts + contracts == 0:
            self.avg_entry_cents = 0.0
        else:
            # Weighted average entry (simplified)
            tot = self.position_contracts + contracts
            self.avg_entry_cents = (
                self.avg_entry_cents * self.position_contracts + price_cents * contracts
            ) / tot
        self.position_contracts += contracts


def match_limit_order(
    intent: TradeIntent,
    snap: MarketSnapshot,
    cfg: PaperFillConfig,
    rng: random.Random | None,
) -> tuple[float, float, float]:
    """Return (filled_contracts, effective_price_cents, edge_estimate_cents) or zeros if no fill.

    Edge estimate: mid at snapshot minus effective price (buy YES: positive if we buy below mid).
    """
    if intent.side != "yes" or intent.action != "buy":
        # Extend here for NO-side / sell research.
        return 0.0, 0.0, 0.0

    limit_cents = float(intent.yes_price_cents)
    ask = snap.yes_ask_dollars * 100.0
    bid = snap.yes_bid_dollars * 100.0
    mid = (bid + ask) / 2.0

    # Buy YES limit fills if price is at/above best ask (simplified crossing model).
    if limit_cents + 1e-9 < ask:
        return 0.0, 0.0, 0.0

    rnd = rng or random.Random()
    p = cfg.fill_probability_if_crossed
    if cfg.deterministic:
        p = 1.0 if p >= 0.5 else 0.0
    elif rnd.random() > p:
        return 0.0, 0.0, 0.0

    filled = intent.count * cfg.partial_fill_fraction
    eff = limit_cents + cfg.slippage_cents_per_contract
    fee_per = cfg.fee_cents_per_contract + cfg.slippage_cents_per_contract
    edge = mid - eff  # positive if we buy cheaper than mid
    return filled, eff, edge * filled


def simulate_fill(
    intent: TradeIntent,
    snap: MarketSnapshot,
    cfg: PaperFillConfig,
    rng: random.Random | None,
) -> tuple[TradeOutcome | None, float]:
    """Produce one `TradeOutcome` and fee cost for a hypothetical fill."""
    filled, eff, edge_total = match_limit_order(intent, snap, cfg, rng)
    if filled <= 0:
        return None, 0.0
    fee_cost = cfg.fee_cents_per_contract * filled
    # Realized PnL stub: we only track edge vs mid at entry for research reports
    pnl = edge_total - fee_cost
    return (
        TradeOutcome(pnl_cents=pnl, edge_estimate_cents=edge_total / filled if filled else 0.0),
        fee_cost,
    )
