"""Scale max contracts and exposure caps from account balance (percentage of balance)."""

from __future__ import annotations

from kalshi_bot.config import Settings


def effective_max_contracts(
    settings: Settings,
    *,
    balance_cents: int | None,
    yes_price_cents: int,
) -> int:
    """Cap order size: min(config max, balance × TRADE_RISK_PCT_OF_BALANCE_PER_TRADE / price)."""
    base = settings.max_contracts_per_market
    if not settings.trade_balance_sizing_enabled or balance_cents is None or balance_cents <= 0:
        return base
    price = max(1, min(99, yes_price_cents))
    budget = float(balance_cents) * settings.trade_risk_pct_of_balance_per_trade
    cap = int(budget // float(price))
    return max(1, min(base, cap))


def effective_max_exposure_cents(settings: Settings, balance_cents: int | None) -> float:
    """Cap total exposure: min(MAX_EXPOSURE_CENTS, balance × TRADE_TOTAL_RISK_PCT_OF_BALANCE)."""
    static = float(settings.max_exposure_cents)
    if not settings.trade_balance_sizing_enabled or balance_cents is None or balance_cents <= 0:
        return static
    scaled = float(balance_cents) * settings.trade_total_risk_pct_of_balance
    return min(static, scaled)
