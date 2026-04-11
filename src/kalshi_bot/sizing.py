"""Scale max contracts and exposure caps from account balance (percentage of balance)."""

from __future__ import annotations

import math
import threading

from kalshi_bot.config import Settings

_LOCK = threading.Lock()
_NOTIONAL_SWEEP_I = 0


def parse_notional_sweep_usd(raw: str | None) -> list[float]:
    """Parse ``TRADE_NOTIONAL_SWEEP_USD`` like ``3,5,7,10`` into positive floats."""
    if not raw or not str(raw).strip():
        return []
    out: list[float] = []
    for part in str(raw).split(","):
        p = part.strip()
        if not p:
            continue
        try:
            v = float(p)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out


def effective_trade_max_order_notional_usd(
    settings: Settings,
    balance_cents: int | None,
) -> float | None:
    """Per-order $ cap at limit: ``(balance/100) × TRADE_RISK_PCT_OF_BALANCE_PER_TRADE`` when balance sizing is on.

    Otherwise uses ``TRADE_MAX_ORDER_NOTIONAL_USD`` (may be ``None`` or ``0`` = no cap, per settings).
    """
    if settings.trade_balance_sizing_enabled and balance_cents is not None and balance_cents > 0:
        return (float(balance_cents) / 100.0) * settings.trade_risk_pct_of_balance_per_trade
    return settings.trade_max_order_notional_usd


def next_buy_yes_notional_min_max(
    settings: Settings,
    *,
    balance_cents: int | None = None,
) -> tuple[float | None, float | None]:
    """Return (min, max) USD notional for this buy-YES order.

    When ``TRADE_NOTIONAL_SWEEP_USD`` is set, each step sets a **cap** (target max $ at limit); the **floor** always
    comes from ``TRADE_MIN_ORDER_NOTIONAL_USD`` only (so 0 = no minimum, sweep does not re-impose a floor).

    When ``TRADE_BALANCE_SIZING_ENABLED`` is true and ``balance_cents`` is set, the max cap follows the same per-trade
    budget as contract sizing (not ``TRADE_MAX_ORDER_NOTIONAL_USD``).
    """
    mn = settings.trade_min_order_notional_usd
    mx = effective_trade_max_order_notional_usd(settings, balance_cents)
    vals = parse_notional_sweep_usd(settings.trade_notional_sweep_usd)
    if not vals:
        return mn, mx
    global _NOTIONAL_SWEEP_I
    with _LOCK:
        t = vals[_NOTIONAL_SWEEP_I % len(vals)]
        _NOTIONAL_SWEEP_I += 1
    if mx is not None and mx > 0:
        cap = min(t, mx)
    else:
        cap = t
    return (mn, cap)


def effective_max_contracts(
    settings: Settings,
    *,
    balance_cents: int | None,
    yes_price_cents: int,
) -> int:
    """Cap order size in contracts.

    With balance sizing and a positive balance: ``floor(balance × TRADE_RISK_PCT_OF_BALANCE_PER_TRADE / price)``,
    or ``0`` if that budget cannot cover one contract at the limit price.

    Otherwise: ``MAX_CONTRACTS_PER_MARKET`` (static fallback when balance is unknown or sizing is off).
    """
    base = settings.max_contracts_per_market
    if not settings.trade_balance_sizing_enabled or balance_cents is None or balance_cents <= 0:
        return base
    price = max(1, min(99, yes_price_cents))
    budget = float(balance_cents) * settings.trade_risk_pct_of_balance_per_trade
    cap = int(budget // float(price))
    return max(0, cap)


def cap_buy_yes_count_for_notional(
    count: int,
    *,
    yes_price_cents: int,
    max_notional_usd: float | None,
    side: str,
    action: str,
) -> int:
    """Cap contracts for buy YES so approximate cash at limit stays ≤ ``max_notional_usd``.

    Uses limit price in dollars as ``yes_price_cents/100`` (same notion as exposure in ``execute_intent``).
    """
    if side != "yes" or action != "buy":
        return count
    if max_notional_usd is None or max_notional_usd <= 0:
        return count
    p = max(1, min(99, yes_price_cents)) / 100.0
    max_n = int(max_notional_usd / p)
    return max(0, min(count, max_n))


def adjust_buy_yes_count_for_notional_floor(
    count: int,
    *,
    yes_price_cents: int,
    min_notional_usd: float | None,
    max_notional_usd: float | None,
    max_contracts: int,
) -> int:
    """Raise buy-YES count to meet minimum $ at limit without exceeding max $ and max_contracts.

    Returns 0 if the minimum notional cannot be reached (e.g. cap too tight vs min).
    """
    if min_notional_usd is None or min_notional_usd <= 0:
        return max(0, count)
    p = max(1, min(99, yes_price_cents)) / 100.0
    max_n = max(0, max_contracts)
    if max_notional_usd is not None and max_notional_usd > 0:
        max_n = min(max_n, int(max_notional_usd / p))
    count = min(max(0, count), max_n)
    need = int(math.ceil(min_notional_usd / p))
    if need > max_n:
        return 0
    return max(count, need)


def effective_max_exposure_cents(settings: Settings, balance_cents: int | None) -> float:
    """Cap total portfolio exposure (cents).

    With ``TRADE_NO_MAX_EXPOSURE_CAP`` and a positive known balance: effectively no cap (infinity); the exchange
    and per-order notional/contract limits still apply. Risk still blocks when balance≤0.

    With balance sizing and a positive balance (otherwise): ``balance × TRADE_TOTAL_RISK_PCT_OF_BALANCE``.

    Otherwise: ``MAX_EXPOSURE_CENTS`` (static fallback when balance is unknown or sizing is off).
    """
    static = float(settings.max_exposure_cents)
    if settings.trade_no_max_exposure_cap:
        if balance_cents is not None and balance_cents > 0:
            return float("inf")
        return static
    if not settings.trade_balance_sizing_enabled or balance_cents is None or balance_cents <= 0:
        return static
    return float(balance_cents) * settings.trade_total_risk_pct_of_balance
