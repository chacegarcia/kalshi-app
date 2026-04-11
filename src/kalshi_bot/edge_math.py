"""Fee-adjusted edge and intra-market boxed (YES+NO) surplus — Kalshi-only.

Interpreting **YES as shares** (see ``trading_model``): ``net_edge_buy_yes_long`` is expected edge
per **share** at the ask (dollars on the \$1 face); multiply by share count for total edge dollars
before fees on a sized order.
"""

from __future__ import annotations

from kalshi_bot.fees import kalshi_general_taker_fee_usd


def implied_yes_ask_dollars(best_no_bid_dollars: float) -> float:
    """Lift YES: ~ 1 − best NO bid (binary complement on the bid book)."""
    return max(0.01, min(0.99, 1.0 - _clamp01(best_no_bid_dollars)))


def implied_no_ask_dollars(best_yes_bid_dollars: float) -> float:
    """Lift NO: ~ 1 − best YES bid."""
    return max(0.01, min(0.99, 1.0 - _clamp01(best_yes_bid_dollars)))


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def boxed_pair_cost_dollars(best_yes_bid_dollars: float, best_no_bid_dollars: float) -> float:
    """Cost to buy 1 YES @ implied YES ask + 1 NO @ implied NO ask (before fees), in dollars."""
    return implied_yes_ask_dollars(best_no_bid_dollars) + implied_no_ask_dollars(best_yes_bid_dollars)


def boxed_arb_surplus_before_fees_dollars(best_yes_bid_dollars: float, best_no_bid_dollars: float) -> float:
    """1.0 − cost; positive means naive boxed arb before fees & rounding."""
    return 1.0 - boxed_pair_cost_dollars(best_yes_bid_dollars, best_no_bid_dollars)


def boxed_arb_surplus_after_taker_fees_dollars(
    best_yes_bid_dollars: float,
    best_no_bid_dollars: float,
    *,
    contracts: int = 1,
) -> float:
    """Surplus after paying taker fee on each leg (same contract count per leg)."""
    ya = implied_yes_ask_dollars(best_no_bid_dollars)
    na = implied_no_ask_dollars(best_yes_bid_dollars)
    pay = ya + na
    fy = kalshi_general_taker_fee_usd(contracts=contracts, price_dollars=ya)
    fn = kalshi_general_taker_fee_usd(contracts=contracts, price_dollars=na)
    return 1.0 - pay - (fy + fn) / max(1, contracts)


def net_edge_buy_yes_long(
    *,
    fair_yes: float,
    yes_ask_dollars: float,
    contracts: int = 1,
) -> float:
    """fair − ask − taker fee per share (all in dollars on \$1 face). ``contracts`` = share count for fee averaging."""
    fy = kalshi_general_taker_fee_usd(contracts=contracts, price_dollars=yes_ask_dollars)
    per = fy / max(1, contracts)
    return fair_yes - yes_ask_dollars - per


def middle_penalty_multiplier(mid: float, *, width: float = 0.15) -> float:
    """Extra edge (dollars) suggested near 0.50 — fees worst at mid; width = distance from 0.5 to start penalty."""
    d = abs(mid - 0.5)
    if d >= width:
        return 0.0
    return width - d


def min_edge_threshold_for_mid(
    mid: float,
    *,
    base_min_edge: float,
    middle_extra: float,
    middle_width: float = 0.15,
) -> float:
    """Require larger edge near 50% (fee + adverse selection heuristic)."""
    return base_min_edge + middle_extra * (middle_penalty_multiplier(mid, width=middle_width) / max(middle_width, 1e-9))
