"""Master bot: combines Python trading stack + learning DB — win-rate targets, 30–90¢ band, scaled size 1–100.

The C# ``azure-wrapper`` is a deployment/host layer; **this module** is the canonical algorithm hook in Python.
No real market guarantees 75% wins — we treat that as a **rolling historical** target for sizing and optional gates.
"""

from __future__ import annotations

from dataclasses import replace

from kalshi_bot.config import Settings
from kalshi_bot.confirmed_bets_db import RollingStats, count_closed, rolling_win_rate
from kalshi_bot.logger import StructuredLogger
from kalshi_bot.strategy import TradeIntent


def master_yes_ask_allowed(settings: Settings, yes_price_cents: int) -> bool:
    """Enforce master entry band (default 30–90¢ implied YES)."""
    if not settings.trade_master_enabled:
        return True
    lo = int(settings.trade_master_yes_ask_min_cents)
    hi = int(settings.trade_master_yes_ask_max_cents)
    return lo <= yes_price_cents <= hi


def _scale_contracts(
    settings: Settings,
    *,
    base: int,
    max_from_risk: int,
    yes_ask_cents: int,
    net_edge: float | None,
    rs: RollingStats,
) -> int:
    """Map rolling performance + edge + price to integer contracts in [1, cap]."""
    cap = min(int(settings.trade_master_max_contracts_cap), max(1, max_from_risk))
    target_wr = float(settings.trade_master_target_win_rate)
    cold_max = max(1, int(settings.trade_master_cold_start_max_contracts))
    n_closed = count_closed(settings)

    if n_closed < int(settings.trade_master_min_closed_bets):
        return max(1, min(base, cold_max, cap))

    wr = rs.win_rate
    if wr is None:
        return max(1, min(base, cold_max, cap))

    if settings.trade_master_hard_block_below_target and wr < target_wr:
        return 0

    # Excess win rate above target → more size; edge and "favorite" ask boost size modestly.
    wr_score = max(0.0, min(1.0, (wr - target_wr) / max(1e-9, 1.0 - target_wr)))
    ne = float(net_edge or 0.0)
    edge_score = max(0.0, min(1.0, ne * float(settings.trade_master_edge_scale_coeff)))
    lo_a = int(settings.trade_master_yes_ask_min_cents)
    hi_a = int(settings.trade_master_yes_ask_max_cents)
    span = max(1, hi_a - lo_a)
    ask_score = max(0.0, min(1.0, (hi_a - yes_ask_cents) / float(span)))

    w_wr = float(settings.trade_master_weight_win_rate)
    w_e = float(settings.trade_master_weight_edge)
    w_a = float(settings.trade_master_weight_ask_favorite)
    combined = w_wr * wr_score + w_e * edge_score + w_a * ask_score
    combined = max(0.0, min(1.0, combined))

    lo = 1
    scaled = lo + int(round(combined * (cap - lo)))
    scaled = max(1, min(scaled, cap, base))
    return scaled


def apply_master_bot_to_intent(
    settings: Settings,
    intent: TradeIntent,
    *,
    log: StructuredLogger,
    max_contracts_from_risk: int,
) -> TradeIntent | None:
    """Return adjusted intent, or None if master gates block the order. Buy-YES only."""
    if not settings.trade_master_enabled:
        return intent
    if intent.action != "buy" or intent.side != "yes":
        return intent

    # Add-on buys choose their own size; still respect band + win-rate gate when master is on.
    if getattr(intent, "position_scale_addon", False):
        yc = int(intent.yes_price_cents)
        if not master_yes_ask_allowed(settings, yc):
            log.info(
                "master_bot_blocked",
                reason="yes_ask_outside_band",
                ticker=intent.ticker,
                yes_price_cents=yc,
                band_min=settings.trade_master_yes_ask_min_cents,
                band_max=settings.trade_master_yes_ask_max_cents,
            )
            return None
        rs = rolling_win_rate(settings, window=int(settings.trade_master_rolling_window))
        if (
            rs.win_rate is not None
            and rs.closed >= int(settings.trade_master_min_closed_bets)
            and rs.win_rate < float(settings.trade_master_target_win_rate)
            and settings.trade_master_hard_block_below_target
        ):
            log.info(
                "master_bot_blocked",
                reason="rolling_win_rate_below_target",
                ticker=intent.ticker,
                win_rate=rs.win_rate,
                target=settings.trade_master_target_win_rate,
                closed=rs.closed,
            )
            return None
        return intent

    yc = int(intent.yes_price_cents)
    if not master_yes_ask_allowed(settings, yc):
        log.info(
            "master_bot_blocked",
            reason="yes_ask_outside_band",
            ticker=intent.ticker,
            yes_price_cents=yc,
            band_min=settings.trade_master_yes_ask_min_cents,
            band_max=settings.trade_master_yes_ask_max_cents,
        )
        return None

    rs = rolling_win_rate(settings, window=int(settings.trade_master_rolling_window))
    if (
        rs.win_rate is not None
        and rs.closed >= int(settings.trade_master_min_closed_bets)
        and rs.win_rate < float(settings.trade_master_target_win_rate)
        and settings.trade_master_hard_block_below_target
    ):
        log.info(
            "master_bot_blocked",
            reason="rolling_win_rate_below_target",
            ticker=intent.ticker,
            win_rate=rs.win_rate,
            target=settings.trade_master_target_win_rate,
            closed=rs.closed,
        )
        return None

    if not settings.trade_master_apply_contract_scaling:
        return intent

    scaled = _scale_contracts(
        settings,
        base=int(intent.count),
        max_from_risk=max_contracts_from_risk,
        yes_ask_cents=yc,
        net_edge=intent.master_net_edge,
        rs=rs,
    )
    if scaled < 1:
        log.info("master_bot_blocked", reason="scaled_to_zero", ticker=intent.ticker)
        return None
    if scaled != intent.count:
        log.info(
            "master_bot_contracts_scaled",
            ticker=intent.ticker,
            before=intent.count,
            after=scaled,
            rolling_win_rate=rs.win_rate,
            net_edge=intent.master_net_edge,
        )
    return replace(intent, count=scaled)
