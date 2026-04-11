"""Application settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env(name: str, py: str) -> AliasChoices:
    """Allow both `NAME` env vars and Python kwargs (`py`) in Settings()."""
    return AliasChoices(name, py)


def _default_log_path() -> Path:
    return Path("logs/kalshi_bot.jsonl")


# Repo root = directory containing pyproject.toml (parent of src/)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """Project root (``pyproject.toml``). Runtime config: ``.env``; variable list: ``.env.example``."""
    return _PROJECT_ROOT


class Settings(BaseSettings):
    """Environment-driven settings (loaded from ``.env``). See ``.env.example`` for every key."""

    model_config = SettingsConfigDict(
        env_file=_PROJECT_ROOT / ".env",
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

    # Optional: pin REST/WebSocket hosts (defaults follow KALSHI_ENV). Prod default is Kalshi's unified API
    # (api.elections.kalshi.com — all markets, not only elections).
    kalshi_rest_base_url: str | None = Field(
        default=None, validation_alias=_env("KALSHI_REST_BASE_URL", "kalshi_rest_base_url")
    )
    kalshi_ws_url: str | None = Field(default=None, validation_alias=_env("KALSHI_WS_URL", "kalshi_ws_url"))

    live_trading: bool = Field(default=False, validation_alias=_env("LIVE_TRADING", "live_trading"))
    dry_run: bool = Field(default=True, validation_alias=_env("DRY_RUN", "dry_run"))

    # --- Risk & bankroll (session limits, exposure caps) ---
    max_exposure_cents: float = Field(
        default=50_000.0,
        ge=0,
        validation_alias=AliasChoices(
            "TRADE_MAX_TOTAL_EXPOSURE_CENTS",
            "MAX_EXPOSURE_CENTS",
            "max_exposure_cents",
        ),
    )
    max_contracts_per_market: int = Field(
        default=10,
        ge=1,
        validation_alias=AliasChoices(
            "TRADE_MAX_CONTRACTS_PER_MARKET",
            "MAX_CONTRACTS_PER_MARKET",
            "MAX_POSITION_CONTRACTS",
            "max_contracts_per_market",
        ),
    )
    max_daily_drawdown_usd: float = Field(
        default=25.0,
        ge=0,
        validation_alias=AliasChoices(
            "TRADE_STOP_MAX_SESSION_LOSS_USD",
            "MAX_DAILY_DRAWDOWN_USD",
            "MAX_DAILY_LOSS_USD",
            "max_daily_drawdown_usd",
        ),
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

    # --- Trading — market (which contract the sample strategy / auto-sell target) ---
    strategy_market_ticker: str = Field(
        default="",
        validation_alias=AliasChoices("TRADE_MARKET_TICKER", "STRATEGY_MARKET_TICKER", "strategy_market_ticker"),
    )

    # --- Trading — entry (buy YES): price filters & size ---
    strategy_max_yes_ask_dollars: float = Field(
        default=0.55,
        ge=0,
        le=1.0,
        validation_alias=AliasChoices(
            "TRADE_BUY_MAX_YES_ASK_DOLLARS",
            "STRATEGY_MAX_YES_ASK_DOLLARS",
            "strategy_max_yes_ask_dollars",
        ),
        description="Max implied YES ask as a fraction of $1 (0–1). Example: 0.55 = 55¢, 0.98 = 98¢. Not a dollar amount like 5.00.",
    )
    strategy_min_spread_dollars: float = Field(
        default=0.0,
        ge=0,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_MIN_SPREAD_DOLLARS",
            "STRATEGY_MIN_SPREAD_DOLLARS",
            "strategy_min_spread_dollars",
        ),
    )
    strategy_probability_gap: float = Field(
        default=0.0,
        ge=0,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_MIN_EDGE_FROM_50",
            "STRATEGY_PROBABILITY_GAP",
            "strategy_probability_gap",
        ),
    )
    strategy_order_count: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices(
            "TRADE_BUY_CONTRACTS_PER_ORDER",
            "STRATEGY_ORDER_COUNT",
            "strategy_order_count",
        ),
    )
    trade_min_order_notional_usd: float | None = Field(
        default=3.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "TRADE_MIN_ORDER_NOTIONAL_USD",
            "trade_min_order_notional_usd",
        ),
        description="Buy YES: bump contracts to at least this $ at limit (0 = no floor). Requires TRADE_MAX_ORDER_NOTIONAL_USD ≥ this.",
    )
    trade_max_order_notional_usd: float | None = Field(
        default=5.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "TRADE_MAX_ORDER_NOTIONAL_USD",
            "trade_max_order_notional_usd",
        ),
        description="Cap buy-YES $ at limit price. Default 5; set 0 to disable cap only.",
    )
    strategy_limit_price_cents: int = Field(
        default=50,
        ge=1,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_BUY_LIMIT_YES_PRICE_CENTS",
            "STRATEGY_LIMIT_PRICE_CENTS",
            "strategy_limit_price_cents",
        ),
    )
    strategy_min_seconds_between_signals: int = Field(
        default=45,
        ge=0,
        validation_alias=AliasChoices(
            "TRADE_MIN_SECONDS_BETWEEN_ORDERS",
            "STRATEGY_MIN_SECONDS_BETWEEN_SIGNALS",
            "strategy_min_seconds_between_signals",
        ),
    )

    # --- Edge-aware entry (fair value vs market + Kalshi taker fee curve) ---
    trade_fair_yes_prob: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("TRADE_FAIR_YES_PROB", "trade_fair_yes_prob"),
    )
    trade_use_edge_strategy: bool = Field(
        default=False,
        validation_alias=AliasChoices("TRADE_USE_EDGE_STRATEGY", "trade_use_edge_strategy"),
    )
    trade_min_net_edge_after_fees: float = Field(
        default=0.005,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices("TRADE_MIN_NET_EDGE_AFTER_FEES", "trade_min_net_edge_after_fees"),
        description="Min (fair_yes − YES_ask − taker fee), 0–1 scale on $1 face. 0.005≈0.5¢ edge; 0.05≈5¢ — looser = more signals.",
    )
    trade_edge_middle_extra_edge: float = Field(
        default=0.002,
        ge=0.0,
        le=0.2,
        validation_alias=AliasChoices("TRADE_EDGE_MIDDLE_EXTRA_EDGE", "trade_edge_middle_extra_edge"),
    )
    trade_llm_screen_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("TRADE_LLM_SCREEN_ENABLED", "trade_llm_screen_enabled"),
    )
    trade_llm_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("TRADE_LLM_MODEL", "trade_llm_model"),
    )
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "openai_api_key"),
    )
    trade_llm_auto_execute: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_LLM_AUTO_EXECUTE",
            "trade_llm_auto_execute",
        ),
    )
    trade_llm_max_markets_per_run: int = Field(
        default=500,
        ge=1,
        le=500,
        validation_alias=AliasChoices(
            "TRADE_LLM_MAX_MARKETS_PER_RUN",
            "trade_llm_max_markets_per_run",
        ),
    )
    trade_min_market_volume: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices(
            "TRADE_MIN_MARKET_VOLUME",
            "trade_min_market_volume",
        ),
        description="If set, skip markets with lower REST `volume` (None/unknown skips when min is set).",
    )
    trade_max_entry_spread_dollars: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "TRADE_MAX_ENTRY_SPREAD_DOLLARS",
            "trade_max_entry_spread_dollars",
        ),
        description="If set, skip when (YES_ask − YES_bid) exceeds this — keeps tighter, more liquid books.",
    )
    trade_llm_relaxed_approval: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_LLM_RELAXED_APPROVAL",
            "trade_llm_relaxed_approval",
        ),
        description="If true, LLM may approve when fair_yes clears edge vs ask; default prompt requires a 'clear mispricing'.",
    )
    trade_llm_discovery_query: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TRADE_LLM_DISCOVERY_QUERY",
            "trade_llm_discovery_query",
        ),
        description="Optional theme for discover-trade: LLM filters titles; empty = allow normal markets.",
    )
    trade_discover_auto_execute: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_DISCOVER_AUTO_EXECUTE",
            "trade_discover_auto_execute",
        ),
        description="If true, discover-trade may submit orders when --execute (still needs LIVE_TRADING and not DRY_RUN).",
    )
    trade_tape_max_trades_fetch: int = Field(
        default=3000,
        ge=50,
        le=50_000,
        validation_alias=AliasChoices(
            "TRADE_TAPE_MAX_TRADES_FETCH",
            "trade_tape_max_trades_fetch",
        ),
        description="How many recent public trades to pull for tape-trade ranking (paginated).",
    )
    trade_tape_top_markets: int = Field(
        default=100,
        ge=1,
        le=500,
        validation_alias=AliasChoices(
            "TRADE_TAPE_TOP_MARKETS",
            "trade_tape_top_markets",
        ),
        description="After ranking by flow, evaluate at most this many tickers per run.",
    )
    trade_tape_min_flow_usd: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "TRADE_TAPE_MIN_FLOW_USD",
            "trade_tape_min_flow_usd",
        ),
        description="Skip tickers with aggregate tape notional below this (0 = off).",
    )
    trade_tape_auto_execute: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_TAPE_AUTO_EXECUTE",
            "trade_tape_auto_execute",
        ),
        description="If true, tape-trade may submit when --execute (still needs LIVE_TRADING and not DRY_RUN).",
    )

    # Balance-scaled limits (bigger account → larger caps within fixed % of balance)
    trade_balance_sizing_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_BALANCE_SIZING_ENABLED",
            "trade_balance_sizing_enabled",
        ),
    )
    trade_risk_pct_of_balance_per_trade: float = Field(
        default=0.02,
        ge=0.0001,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_RISK_PCT_OF_BALANCE_PER_TRADE",
            "trade_risk_pct_of_balance_per_trade",
        ),
    )
    trade_total_risk_pct_of_balance: float = Field(
        default=0.25,
        ge=0.01,
        le=1.0,
        validation_alias=AliasChoices(
            "TRADE_TOTAL_RISK_PCT_OF_BALANCE",
            "trade_total_risk_pct_of_balance",
        ),
    )

    # --- Trading — exit: take-profit (auto-sell) & pacing ---
    auto_sell_min_yes_bid_cents: int | None = Field(
        default=None,
        ge=1,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_TAKE_PROFIT_MIN_YES_BID_CENTS",
            "AUTO_SELL_MIN_YES_BID_CENTS",
            "auto_sell_min_yes_bid_cents",
        ),
    )
    auto_sell_poll_seconds: float = Field(
        default=2.0,
        ge=0.5,
        validation_alias=AliasChoices(
            "TRADE_TAKE_PROFIT_POLL_SECONDS",
            "AUTO_SELL_POLL_SECONDS",
            "auto_sell_poll_seconds",
        ),
    )

    # Exit quality: implied-% floor, optional profit-margin vs entry, IOC-style sells (Kalshi API TIF)
    trade_exit_take_profit_min_yes_bid_pct: float = Field(
        default=72.0,
        ge=1.0,
        le=99.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TAKE_PROFIT_MIN_YES_BID_PCT",
            "trade_exit_take_profit_min_yes_bid_pct",
        ),
    )
    trade_exit_min_profit_cents_per_contract: float | None = Field(
        default=None,
        ge=0.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_MIN_PROFIT_CENTS_PER_CONTRACT",
            "TRADE_EXIT_MIN_PROFIT_CENTS",
            "trade_exit_min_profit_cents_per_contract",
        ),
    )
    trade_exit_entry_reference_yes_cents: int | None = Field(
        default=None,
        ge=1,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_EXIT_ENTRY_REFERENCE_YES_CENTS",
            "trade_exit_entry_reference_yes_cents",
        ),
    )
    trade_exit_estimate_entry_from_portfolio: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_EXIT_ESTIMATE_ENTRY_FROM_PORTFOLIO",
            "trade_exit_estimate_entry_from_portfolio",
        ),
    )
    trade_exit_only_profit_margin: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_EXIT_ONLY_PROFIT_MARGIN",
            "trade_exit_only_profit_margin",
        ),
    )
    trade_exit_sell_time_in_force: Literal["immediate_or_cancel", "fill_or_kill", "good_till_canceled"] = Field(
        default="immediate_or_cancel",
        validation_alias=AliasChoices(
            "TRADE_EXIT_SELL_TIME_IN_FORCE",
            "trade_exit_sell_time_in_force",
        ),
    )
    trade_exit_sell_aggression_cents: int = Field(
        default=0,
        ge=0,
        le=15,
        validation_alias=AliasChoices(
            "TRADE_EXIT_SELL_AGGRESSION_CENTS",
            "trade_exit_sell_aggression_cents",
        ),
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

    @field_validator("kalshi_rest_base_url", "kalshi_ws_url", mode="before")
    @classmethod
    def _blank_url_to_none(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def _blank_openai_to_none(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    @field_validator("trade_exit_sell_time_in_force", mode="before")
    @classmethod
    def _normalize_exit_time_in_force(cls, v: object) -> str:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return "immediate_or_cancel"
        s = str(v).strip().lower().replace("-", "_")
        aliases = {
            "ioc": "immediate_or_cancel",
            "fok": "fill_or_kill",
            "gtc": "good_till_canceled",
            "good_till_cancelled": "good_till_canceled",
        }
        s = aliases.get(s, s)
        if s not in ("immediate_or_cancel", "fill_or_kill", "good_till_canceled"):
            raise ValueError(
                "trade_exit_sell_time_in_force must be immediate_or_cancel, fill_or_kill, or good_till_canceled "
                f"(got {v!r})"
            )
        return s

    @field_validator(
        "live_trading",
        "dry_run",
        "kill_switch",
        "no_martingale",
        "dashboard_enabled",
        "dashboard_open_browser",
        "trade_use_edge_strategy",
        "trade_llm_screen_enabled",
        "trade_llm_auto_execute",
        "trade_llm_relaxed_approval",
        "trade_discover_auto_execute",
        "trade_tape_auto_execute",
        "trade_balance_sizing_enabled",
        mode="before",
    )
    @classmethod
    def _parse_bool(cls, v: object) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        s = str(v).strip().lower()
        return s in ("1", "true", "yes", "on")

    @model_validator(mode="after")
    def _order_notional_min_max(self) -> "Settings":
        mn = self.trade_min_order_notional_usd
        mx = self.trade_max_order_notional_usd
        if mn is not None and mx is not None and mn > 0 and mx > 0 and mn > mx:
            raise ValueError(
                "TRADE_MIN_ORDER_NOTIONAL_USD must be <= TRADE_MAX_ORDER_NOTIONAL_USD when both are positive"
            )
        return self

    @property
    def rest_base_url(self) -> str:
        if self.kalshi_rest_base_url:
            return self.kalshi_rest_base_url.rstrip("/")
        if self.kalshi_env == "demo":
            return "https://demo-api.kalshi.co/trade-api/v2"
        return "https://api.elections.kalshi.com/trade-api/v2"

    @property
    def ws_url(self) -> str:
        if self.kalshi_ws_url:
            return self.kalshi_ws_url.rstrip("/")
        if self.kalshi_env == "demo":
            return "wss://demo-api.kalshi.co/trade-api/ws/v2"
        return "wss://api.elections.kalshi.com/trade-api/ws/v2"

    @property
    def can_send_real_orders(self) -> bool:
        """True only when live trading is explicitly enabled and dry-run is off."""
        return self.live_trading and not self.dry_run

    @property
    def trade_buy_max_yes_ask_implied_pct(self) -> float:
        """Entry cap `TRADE_BUY_MAX_YES_ASK_DOLLARS` as implied YES probability in 0–100 (e.g. 0.55 → 55)."""
        return self.strategy_max_yes_ask_dollars * 100.0

    @property
    def trade_entry_min_edge_from_50_pct_points(self) -> float:
        """Minimum |mid−50%| in percentage points (`TRADE_ENTRY_MIN_EDGE_FROM_50` × 100)."""
        return self.strategy_probability_gap * 100.0

    def auto_sell_effective_min_yes_bid_cents(self, cli_override: int | None) -> int | None:
        """Min best YES bid (cents) to treat as take-profit-by-implied-%, or None if only profit-margin mode."""
        if self.trade_exit_only_profit_margin:
            return None
        if cli_override is not None:
            return cli_override
        if self.auto_sell_min_yes_bid_cents is not None:
            return self.auto_sell_min_yes_bid_cents
        return int(round(self.trade_exit_take_profit_min_yes_bid_pct))


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
