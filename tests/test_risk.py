"""Risk gate tests (no network)."""

from kalshi_bot.config import Settings
from kalshi_bot.risk import RiskManager


def _settings(**kwargs: object) -> Settings:
    base = {
        "kalshi_api_key_id": "test",
        "kalshi_env": "demo",
        "dry_run": True,
        "live_trading": False,
        "max_contracts_per_market": 10,
        "max_exposure_cents": 1_000_000.0,
        "max_daily_drawdown_usd": 25.0,
        "max_open_orders_per_market": 3,
        "cooldown_after_loss_seconds": 300,
        "kill_switch": False,
        "no_martingale": False,
    }
    base.update(kwargs)
    return Settings.model_validate(base)


def test_kill_switch_blocks() -> None:
    s = _settings(kill_switch=True)
    r = RiskManager(s)
    d = r.check_new_order(
        market_ticker="KXTEST",
        order_contracts=1,
        position_contracts_for_market=0.0,
        resting_orders_on_market=0,
    )
    assert not d.allowed
    assert "kill_switch" in d.reason


def test_max_orders_per_market() -> None:
    s = _settings(max_open_orders_per_market=2)
    r = RiskManager(s)
    d = r.check_new_order(
        market_ticker="KXTEST",
        order_contracts=1,
        position_contracts_for_market=0.0,
        resting_orders_on_market=2,
    )
    assert not d.allowed


def test_daily_drawdown_blocks() -> None:
    s = _settings(max_daily_drawdown_usd=10.0)
    r = RiskManager(s)
    r.state.session_start_balance_cents = 10_000
    r.state.last_balance_cents = 9_000
    d = r.check_new_order(
        market_ticker="KXTEST",
        order_contracts=1,
        position_contracts_for_market=0.0,
        resting_orders_on_market=0,
    )
    assert not d.allowed
    assert "drawdown" in d.reason
