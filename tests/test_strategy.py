"""Strategy unit tests."""

from kalshi_bot.config import Settings
from kalshi_bot.strategy import SampleSpreadGapStrategy, SampleThresholdStrategy


def test_sample_strategy_emits_intent() -> None:
    s = Settings.model_validate(
        {
            "kalshi_api_key_id": "x",
            "kalshi_env": "demo",
            "strategy_market_ticker": "KXFOO",
            "strategy_max_yes_ask_dollars": 0.60,
            "strategy_min_spread_dollars": 0.0,
            "strategy_probability_gap": 0.0,
            "strategy_order_count": 2,
            "strategy_limit_price_cents": 40,
        }
    )
    strat = SampleSpreadGapStrategy(s)
    msg = {
        "type": "ticker",
        "msg": {
            "market_ticker": "KXFOO",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.50",
        },
    }
    intent = strat.on_ticker_message(msg)
    assert intent is not None
    assert intent.ticker == "KXFOO"
    assert intent.count == 2
    assert intent.yes_price_cents == 40


def test_sample_strategy_no_match_high_ask() -> None:
    s = Settings.model_validate(
        {
            "kalshi_api_key_id": "x",
            "kalshi_env": "demo",
            "strategy_market_ticker": "KXFOO",
            "strategy_max_yes_ask_dollars": 0.40,
            "strategy_min_spread_dollars": 0.0,
            "strategy_probability_gap": 0.0,
        }
    )
    strat = SampleThresholdStrategy(s)
    msg = {
        "type": "ticker",
        "msg": {
            "market_ticker": "KXFOO",
            "yes_bid_dollars": "0.10",
            "yes_ask_dollars": "0.90",
        },
    }
    assert strat.on_ticker_message(msg) is None
