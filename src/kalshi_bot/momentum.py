"""YES price momentum from REST candlesticks — buy when recent chart is rising quickly."""

from __future__ import annotations

from kalshi_bot.config import Settings
from kalshi_bot.strategy import TradeIntent, skip_buy_yes_longshot


def yes_price_momentum_is_hot(closes: list[float], settings: Settings) -> tuple[bool, str]:
    """True when the last *short* window of trade-based YES closes rises fast enough."""
    if not settings.trade_momentum_enabled:
        return False, "disabled"
    min_c = settings.trade_momentum_min_candles
    if len(closes) < min_c:
        return False, f"need>={min_c} candles with trades, got {len(closes)}"

    sw = max(2, settings.trade_momentum_short_candles)
    seg = closes[-min(sw, len(closes)) :]
    if len(seg) < 2:
        return False, "short segment too short"

    net = seg[-1] - seg[0]
    per = net / max(len(seg) - 1, 1)
    if net < settings.trade_momentum_min_net_rise_dollars:
        return False, f"net rise ${net:.4f} < min ${settings.trade_momentum_min_net_rise_dollars}"
    if per < settings.trade_momentum_min_rise_per_candle_dollars:
        return False, f"$/candle ${per:.4f} < min ${settings.trade_momentum_min_rise_per_candle_dollars}"
    return True, f"hot net=${net:.4f} avg≈${per:.4f}/candle over {len(seg)} bars"


def momentum_buy_intent_if_hot(
    *,
    ticker: str,
    yes_bid_dollars: float,
    yes_ask_dollars: float,
    settings: Settings,
    close_prices: list[float],
) -> tuple[TradeIntent | None, str]:
    """Buy YES at the implied ask when prior candle closes show a strong uptrend (scalp / mark-to-market)."""
    if not settings.trade_momentum_enabled:
        return None, "momentum disabled"
    hot, why = yes_price_momentum_is_hot(close_prices, settings)
    if not hot:
        return None, why
    if yes_ask_dollars > settings.trade_entry_effective_max_yes_ask_dollars:
        return None, f"ask {yes_ask_dollars:.3f} > max_yes_ask {settings.trade_entry_effective_max_yes_ask_dollars}"
    spread = max(0.0, yes_ask_dollars - yes_bid_dollars)
    if spread < settings.strategy_min_spread_dollars:
        return None, "spread below min (momentum still needs a quotable book)"
    if settings.trade_max_entry_spread_dollars is not None and spread > settings.trade_max_entry_spread_dollars:
        return None, "spread too wide"
    limit_cents = int(max(1, min(99, round(yes_ask_dollars * 100.0))))
    if skip_buy_yes_longshot(settings, limit_cents):
        return None, (
            f"YES ask {limit_cents}¢ below effective min "
            f"{settings.trade_entry_effective_min_yes_ask_cents}¢ (American/longshot gate)"
        )
    return (
        TradeIntent(
            ticker=ticker,
            side="yes",
            action="buy",
            count=settings.strategy_order_count,
            yes_price_cents=limit_cents,
        ),
        why,
    )
