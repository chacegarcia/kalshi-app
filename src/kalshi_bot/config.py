"""Application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env(name: str, py: str) -> AliasChoices:
    """Allow both `NAME` env vars and Python kwargs (`py`) in Settings()."""
    return AliasChoices(name, py)


def _default_log_path() -> Path:
    return Path("logs/kalshi_bot.jsonl")


class Settings(BaseSettings):
    """All configuration is environment-driven; defaults favor demo + dry-run safety."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kalshi_api_key_id: str = Field(default="", validation_alias=_env("KALSHI_API_KEY_ID", "kalshi_api_key_id"))
    kalshi_private_key_path: str | None = Field(
        default=None, validation_alias=_env("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key_path")
    )
    kalshi_private_key_pem: str | None = Field(
        default=None, validation_alias=_env("KALSHI_PRIVATE_KEY_PEM", "kalshi_private_key_pem")
    )

    kalshi_env: Literal["demo", "prod"] = Field(default="demo", validation_alias=_env("KALSHI_ENV", "kalshi_env"))

    live_trading: bool = Field(default=False, validation_alias=_env("LIVE_TRADING", "live_trading"))
    dry_run: bool = Field(default=True, validation_alias=_env("DRY_RUN", "dry_run"))

    # Risk: exposure & drawdown (amounts in USD where noted; cents for exposure cap)
    max_exposure_cents: float = Field(
        default=50_000.0,
        ge=0,
        validation_alias=_env("MAX_EXPOSURE_CENTS", "max_exposure_cents"),
    )
    max_contracts_per_market: int = Field(
        default=10,
        ge=1,
        validation_alias=AliasChoices("MAX_CONTRACTS_PER_MARKET", "MAX_POSITION_CONTRACTS", "max_contracts_per_market"),
    )
    max_daily_drawdown_usd: float = Field(
        default=25.0,
        ge=0,
        validation_alias=AliasChoices("MAX_DAILY_DRAWDOWN_USD", "MAX_DAILY_LOSS_USD", "max_daily_drawdown_usd"),
    )
    max_open_orders_per_market: int = Field(
        default=3, ge=1, validation_alias=_env("MAX_OPEN_ORDERS_PER_MARKET", "max_open_orders_per_market")
    )
    cooldown_after_loss_seconds: int = Field(
        default=300, ge=0, validation_alias=_env("COOLDOWN_AFTER_LOSS_SECONDS", "cooldown_after_loss_seconds")
    )
    loss_streak_threshold: int = Field(default=3, ge=1, validation_alias=_env("LOSS_STREAK_THRESHOLD", "loss_streak_threshold"))
    cooldown_after_loss_streak_seconds: int = Field(
        default=900, ge=0, validation_alias=_env("COOLDOWN_AFTER_LOSS_STREAK_SECONDS", "cooldown_after_loss_streak_seconds")
    )
    no_martingale: bool = Field(default=True, validation_alias=_env("NO_MARTINGALE", "no_martingale"))
    stale_order_seconds: int = Field(default=3600, ge=1, validation_alias=_env("STALE_ORDER_SECONDS", "stale_order_seconds"))
    kill_switch: bool = Field(default=False, validation_alias=_env("KILL_SWITCH", "kill_switch"))

    # Strategy (research sample — plug in your own in strategy.py)
    strategy_market_ticker: str = Field(default="", validation_alias=_env("STRATEGY_MARKET_TICKER", "strategy_market_ticker"))
    strategy_max_yes_ask_dollars: float = Field(
        default=0.55, ge=0, validation_alias=_env("STRATEGY_MAX_YES_ASK_DOLLARS", "strategy_max_yes_ask_dollars")
    )
    strategy_min_spread_dollars: float = Field(
        default=0.0, ge=0, validation_alias=_env("STRATEGY_MIN_SPREAD_DOLLARS", "strategy_min_spread_dollars")
    )
    strategy_probability_gap: float = Field(
        default=0.0,
        ge=0,
        le=0.5,
        validation_alias=_env("STRATEGY_PROBABILITY_GAP", "strategy_probability_gap"),
    )
    strategy_order_count: int = Field(default=1, ge=1, validation_alias=_env("STRATEGY_ORDER_COUNT", "strategy_order_count"))
    strategy_limit_price_cents: int = Field(
        default=50, ge=1, le=99, validation_alias=_env("STRATEGY_LIMIT_PRICE_CENTS", "strategy_limit_price_cents")
    )
    strategy_min_seconds_between_signals: int = Field(
        default=45, ge=0, validation_alias=_env("STRATEGY_MIN_SECONDS_BETWEEN_SIGNALS", "strategy_min_seconds_between_signals")
    )

    # Paper / backtest defaults (override in CLI or code)
    paper_fee_cents_per_contract: float = Field(default=0.0, validation_alias=_env("PAPER_FEE_CENTS_PER_CONTRACT", "paper_fee_cents_per_contract"))
    paper_slippage_cents_per_contract: float = Field(default=0.0, validation_alias=_env("PAPER_SLIPPAGE_CENTS_PER_CONTRACT", "paper_slippage_cents_per_contract"))
    paper_fill_probability: float = Field(default=0.85, ge=0, le=1, validation_alias=_env("PAPER_FILL_PROBABILITY", "paper_fill_probability"))

    # Local monitor (Flask) while `run` is active
    dashboard_enabled: bool = Field(default=True, validation_alias=_env("DASHBOARD_ENABLED", "dashboard_enabled"))
    dashboard_host: str = Field(default="127.0.0.1", validation_alias=_env("DASHBOARD_HOST", "dashboard_host"))
    dashboard_port: int = Field(default=5050, ge=1, le=65535, validation_alias=_env("DASHBOARD_PORT", "dashboard_port"))
    dashboard_open_browser: bool = Field(
        default=True, validation_alias=_env("DASHBOARD_OPEN_BROWSER", "dashboard_open_browser")
    )

    log_level: str = Field(default="INFO", validation_alias=_env("LOG_LEVEL", "log_level"))
    structured_log_path: Path = Field(
        default_factory=_default_log_path, validation_alias=_env("STRUCTURED_LOG_PATH", "structured_log_path")
    )

    @field_validator("live_trading", "dry_run", "kill_switch", "no_martingale", "dashboard_enabled", "dashboard_open_browser", mode="before")
    @classmethod
    def _parse_bool(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        s = str(v).strip().lower()
        return s in ("1", "true", "yes", "on")

    @property
    def rest_base_url(self) -> str:
        if self.kalshi_env == "demo":
            return "https://demo-api.kalshi.co/trade-api/v2"
        return "https://api.elections.kalshi.com/trade-api/v2"

    @property
    def ws_url(self) -> str:
        if self.kalshi_env == "demo":
            return "wss://demo-api.kalshi.co/trade-api/ws/v2"
        return "wss://api.elections.kalshi.com/trade-api/ws/v2"

    @property
    def can_send_real_orders(self) -> bool:
        """True only when live trading is explicitly enabled and dry-run is off."""
        return self.live_trading and not self.dry_run


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
