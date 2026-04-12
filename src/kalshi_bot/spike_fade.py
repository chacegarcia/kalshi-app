"""Detect short-term YES price spikes (REST candlesticks) to avoid chasing momentum."""

from __future__ import annotations

from kalshi_bot.config import Settings


def detect_yes_spike_up(closes: list[float], settings: Settings) -> tuple[bool, str]:
    """True when the last *short* window of trade-based YES closes rose fast (chase risk / fade target)."""
    if not settings.trade_spike_fade_enabled:
        return False, "disabled"
    min_c = settings.trade_spike_fade_min_candles
    if len(closes) < min_c:
        return False, f"need>={min_c} candles with trades, got {len(closes)}"

    sw = max(2, settings.trade_spike_fade_short_candles)
    seg = closes[-min(sw, len(closes)) :]
    if len(seg) < 2:
        return False, "short segment too short"

    net = seg[-1] - seg[0]
    if net >= settings.trade_spike_fade_min_net_rise_dollars:
        return True, f"spike net=${net:.4f} over {len(seg)} bars"
    return False, f"no spike net=${net:.4f} < min ${settings.trade_spike_fade_min_net_rise_dollars}"
