"""Application settings loaded from environment variables."""

from __future__ import annotations

import math
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
        description="Max total exposure (cents) when TRADE_BALANCE_SIZING_ENABLED=false or balance is unknown. With balance sizing + live balance, cap is balance×TRADE_TOTAL_RISK_PCT_OF_BALANCE instead.",
    )
    max_contracts_per_market: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices(
            "TRADE_MAX_CONTRACTS_PER_MARKET",
            "TRADE_MAX_SHARES_PER_MARKET",
            "MAX_CONTRACTS_PER_MARKET",
            "MAX_POSITION_CONTRACTS",
            "max_contracts_per_market",
        ),
        description=(
            "Max YES contracts per market (env clamped to 1–99). "
            "Effective buy cap = this × session order-size multiplier (1–10) from the dashboard; balance sizing can lower it."
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
        description="Used for loss-step cooldown gating in RiskManager; order blocking uses balance≤0, not this USD cap.",
    )
    max_open_orders_per_market: int = Field(
        default=3, ge=1, validation_alias=_env("MAX_OPEN_ORDERS_PER_MARKET", "max_open_orders_per_market")
    )
    cooldown_after_loss_seconds: int = Field(
        default=0, ge=0, validation_alias=_env("COOLDOWN_AFTER_LOSS_SECONDS", "cooldown_after_loss_seconds")
    )
    loss_streak_threshold: int = Field(default=3, ge=1, validation_alias=_env("LOSS_STREAK_THRESHOLD", "loss_streak_threshold"))
    cooldown_after_loss_streak_seconds: int = Field(
        default=0, ge=0, validation_alias=_env("COOLDOWN_AFTER_LOSS_STREAK_SECONDS", "cooldown_after_loss_streak_seconds")
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
        default=0.90,
        ge=0,
        le=1.0,
        validation_alias=AliasChoices(
            "TRADE_BUY_MAX_YES_ASK_DOLLARS",
            "STRATEGY_MAX_YES_ASK_DOLLARS",
            "strategy_max_yes_ask_dollars",
        ),
        description="Max implied YES ask as a fraction of $1 (0–1). Effective entry cap is min(this, TRADE_ENTRY_HARD_MAX_YES_ASK_CENTS/100).",
    )
    trade_entry_hard_max_yes_ask_cents: int = Field(
        default=90,
        ge=1,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_HARD_MAX_YES_ASK_CENTS",
            "trade_entry_hard_max_yes_ask_cents",
        ),
        description=(
            "Hard ceiling (¢): never buy YES/NO with limit price above this. Effective max ask = "
            "min(STRATEGY_MAX_YES_ASK_DOLLARS, this/100). Default 90 blocks entries above 90¢ even if max dollars is higher."
        ),
    )
    strategy_min_spread_dollars: float = Field(
        default=0.0,
        ge=0,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_MIN_SPREAD_DOLLARS",
            "STRATEGY_MIN_SPREAD_DOLLARS",
            "strategy_min_spread_dollars",
        ),
        description="Require at least this YES bid–ask width (0–1 on $1). 0 = do not block on spread tightness.",
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
        description="Require |mid−50%| ≥ this (0–1 scale). 0.02 ≈ skip mids in ~48–52¢ band; 0 = allow coin-flip mids (more signals).",
    )
    trade_entry_min_yes_ask_cents: int = Field(
        default=40,
        ge=0,
        le=98,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_MIN_YES_ASK_CENTS",
            "TRADE_ENTRY_MIN_YES_CHANCE_PCT",
            "trade_entry_min_yes_ask_cents",
        ),
        description=(
            "Minimum implied YES at entry: same number Kalshi shows as “chance” (%%) on the contract — 45 = 45%% ≈ 45¢ ask. "
            "0 = disable this floor (flow/tape only). Stricter of this and TRADE_ENTRY_MAX_AMERICAN_ODDS_YES when that is set."
        ),
    )
    trade_entry_max_american_odds_yes: float = Field(
        default=200.0,
        ge=0.0,
        le=100_000.0,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_MAX_AMERICAN_ODDS_YES",
            "trade_entry_max_american_odds_yes",
        ),
        description=(
            "If >0: skip buy YES when implied American long odds exceed this (+200≈34¢ min ask). "
            "0 = disable this gate. Use TRADE_ENTRY_MIN_YES_ASK_CENTS for an additional hard floor."
        ),
    )
    trade_entry_skip_ticker_substrings: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRADE_ENTRY_SKIP_TICKER_SUBSTRINGS",
            "trade_entry_skip_ticker_substrings",
        ),
        description=(
            "Comma-separated substrings; skip buy YES when the market ticker contains any token (case-insensitive). "
            "Example: MASTERS,PGA — blocks whole event families without per-market tuning."
        ),
    )
    trade_entry_cap_long_yes_substring: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRADE_ENTRY_CAP_LONG_YES_SUBSTRING",
            "trade_entry_cap_long_yes_substring",
        ),
        description=(
            "Used with TRADE_ENTRY_CAP_LONG_YES_MAX: only applies to tickers containing this substring (case-insensitive). "
            "Example: MASTERS — cap how many distinct long-YES names you hold in that family."
        ),
    )
    trade_entry_cap_long_yes_max: int = Field(
        default=0,
        ge=0,
        le=500,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_CAP_LONG_YES_MAX",
            "trade_entry_cap_long_yes_max",
        ),
        description=(
            "If >0 with CAP substring set: skip new buy YES when you already hold long YES in this many distinct tickers "
            "matching the substring (0 = off). Stops many tiny-probability legs from stacking exposure."
        ),
    )
    trade_entry_theta_decay_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_THETA_DECAY_ENABLED",
            "trade_entry_theta_decay_enabled",
        ),
        description=(
            "If true: skip buy YES when time-to-resolution is short (options-style theta) and implied YES is in the "
            "long-shot band below — see TRADE_ENTRY_THETA_*."
        ),
    )
    trade_entry_theta_seconds_to_close_max: int = Field(
        default=3_600,
        ge=60,
        le=31_536_000,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_THETA_SECONDS_TO_CLOSE_MAX",
            "trade_entry_theta_seconds_to_close_max",
        ),
        description=(
            "Theta **long-shot band only** (TRADE_ENTRY_THETA_MIN/MAX_YES_ASK_CENTS): skip that entry when "
            "time-to-resolution ≤ this many seconds. Does **not** limit how far out you trade otherwise — set "
            "TRADE_ENTRY_MAX_SECONDS_UNTIL_RESOLUTION to reject markets that resolve too late (e.g. days away)."
        ),
    )
    trade_entry_theta_min_yes_ask_cents: int = Field(
        default=1,
        ge=1,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_THETA_MIN_YES_ASK_CENTS",
            "trade_entry_theta_min_yes_ask_cents",
        ),
        description="Lower bound (¢) of the long-shot band combined with theta time gate.",
    )
    trade_entry_theta_max_yes_ask_cents: int = Field(
        default=10,
        ge=1,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_THETA_MAX_YES_ASK_CENTS",
            "trade_entry_theta_max_yes_ask_cents",
        ),
        description="Upper bound (¢) of the long-shot band combined with theta time gate (e.g. 10 ≈ 10% implied).",
    )
    trade_entry_event_top_n: int = Field(
        default=0,
        ge=0,
        le=500,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_EVENT_TOP_N",
            "trade_entry_event_top_n",
        ),
        description=(
            "If >0 with EVENT_TOP_N_SUBSTRING: only allow buy YES when this ticker is among the top N markets in the "
            "same event by implied YES (REST yes_ask / last trade). 0 = off."
        ),
    )
    trade_entry_event_top_n_substring: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRADE_ENTRY_EVENT_TOP_N_SUBSTRING",
            "trade_entry_event_top_n_substring",
        ),
        description=(
            "Substring filter for event top-N (case-insensitive). Example: MASTERS — only the top implied YES names "
            "in that event, not every player market."
        ),
    )
    trade_entry_market_intelligence_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_MARKET_INTELLIGENCE_ENABLED",
            "trade_entry_market_intelligence_enabled",
        ),
        description=(
            "If true: detect binary (single open market in event) vs multi-choice (2+ open markets). "
            "Multi-choice: only allow buys on the top-N outcomes by REST implied YES and only if orderbook implied YES "
            "≥ TRADE_ENTRY_MULTI_CHOICE_MIN_YES_ASK_CENTS."
        ),
    )
    trade_entry_multi_choice_top_n: int = Field(
        default=3,
        ge=1,
        le=100,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_MULTI_CHOICE_TOP_N",
            "trade_entry_multi_choice_top_n",
        ),
        description="Multi-choice events only: allow buy YES only for tickers ranked in the top N by implied YES (REST).",
    )
    trade_entry_multi_choice_min_yes_ask_cents: int = Field(
        default=50,
        ge=1,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_MULTI_CHOICE_MIN_YES_ASK_CENTS",
            "trade_entry_multi_choice_min_yes_ask_cents",
        ),
        description=(
            "Multi-choice events only: minimum orderbook implied YES (¢) — same scale as Kalshi 'chance' (e.g. 50 = 50%). "
            "Binary (single-market) events use TRADE_ENTRY_MIN_YES_ASK_CENTS only."
        ),
    )
    trade_entry_max_seconds_until_resolution: float = Field(
        default=0.0,
        ge=0.0,
        le=87658176.0,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_MAX_SECONDS_UNTIL_RESOLUTION",
            "trade_entry_max_seconds_until_resolution",
        ),
        description=(
            "If >0: skip buy entries when the soonest Kalshi close/expiration is **more than** this many seconds away "
            "(only trade markets resolving within this horizon). Example: 43200 = 12 hours. 0 = off. "
            "If resolution time is unknown from the API, the market is **not** skipped."
        ),
    )
    trade_entry_prefer_higher_odds_side_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_PREFER_HIGHER_ODDS_SIDE_ENABLED",
            "trade_entry_prefer_higher_odds_side_enabled",
        ),
        description=(
            "If true: on each binary market, compare YES vs NO lift (bid/ask) and pick the better-scoring leg "
            "(see TRADE_ENTRY_SIDE_CHOICE_SPREAD_PENALTY). LLM/tape/discover/bitcoin use the same rule."
        ),
    )
    trade_entry_side_choice_spread_penalty: float = Field(
        default=0.5,
        ge=0.0,
        le=20.0,
        validation_alias=AliasChoices(
            "TRADE_ENTRY_SIDE_CHOICE_SPREAD_PENALTY",
            "trade_entry_side_choice_spread_penalty",
        ),
        description=(
            "When PREFER_HIGHER_ODDS_SIDE is on: each leg’s score = implied_ask_cents − penalty × (ask−bid) spread in ¢. "
            "Higher score wins (favors the favorite, discounts wide books). 0 = compare asks only (ignores spread)."
        ),
    )
    strategy_order_count: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices(
            "TRADE_BUY_CONTRACTS_PER_ORDER",
            "TRADE_BUY_SHARES_PER_ORDER",
            "STRATEGY_ORDER_COUNT",
            "strategy_order_count",
        ),
        description=(
            "Default shares per buy for rule-based runners (discover/tape/bitcoin); env clamped 1–99. "
            "Session order-size multiplier (1–10, dashboard) scales this in execute_intent: final count = this × multiplier. "
            "llm-trade uses model `shares` up to max_contracts_per_market (pre-mult cap)."
        ),
    )
    trade_min_order_notional_usd: float | None = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "TRADE_MIN_ORDER_NOTIONAL_USD",
            "trade_min_order_notional_usd",
        ),
        description="Buy YES: bump share count to at least this $ notional at limit (0 = no floor). Requires TRADE_MAX_ORDER_NOTIONAL_USD ≥ this when both positive.",
    )
    trade_max_order_notional_usd: float | None = Field(
        default=10.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "TRADE_MAX_ORDER_NOTIONAL_USD",
            "trade_max_order_notional_usd",
        ),
        description="Cap buy-YES $ at limit when TRADE_BALANCE_SIZING_ENABLED=false or balance unknown. With balance sizing + balance, per-order cap is balance×TRADE_RISK_PCT_OF_BALANCE_PER_TRADE (USD). Set 0 to disable static cap when sizing is off.",
    )
    trade_notional_sweep_usd: str | None = Field(
        default="1,3,5,7,10",
        validation_alias=AliasChoices(
            "TRADE_NOTIONAL_SWEEP_USD",
            "trade_notional_sweep_usd",
        ),
        description="Comma-separated USD caps per order (round-robin); min floor is only TRADE_MIN_ORDER_NOTIONAL_USD. Empty = use TRADE_MIN/MAX_ORDER_NOTIONAL_USD only.",
    )
    strategy_limit_price_cents: int = Field(
        default=78,
        ge=1,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_BUY_LIMIT_YES_PRICE_CENTS",
            "STRATEGY_LIMIT_PRICE_CENTS",
            "strategy_limit_price_cents",
        ),
    )
    strategy_min_seconds_between_signals: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices(
            "TRADE_MIN_SECONDS_BETWEEN_ORDERS",
            "STRATEGY_MIN_SECONDS_BETWEEN_SIGNALS",
            "strategy_min_seconds_between_signals",
        ),
        description="WebSocket `run` command only: min seconds between signals. 0 = no spacing.",
    )
    trade_submit_spacing_seconds: float = Field(
        default=5.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "TRADE_SUBMIT_SPACING_SECONDS",
            "trade_submit_spacing_seconds",
        ),
        description="After each submitted buy YES (dry-run or live), sleep this many seconds. 0 = off.",
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
        default=0.002,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices("TRADE_MIN_NET_EDGE_AFTER_FEES", "trade_min_net_edge_after_fees"),
        description="Min (fair_yes − YES_ask − taker fee), 0–1 on $1 face. Used when TRADE_USE_EDGE_STRATEGY + fair value; also LLM base unless TRADE_LLM_MIN_NET_EDGE_AFTER_FEES set. Lower = more passes (e.g. 0.002 ≈ 0.2¢/share).",
    )
    trade_edge_middle_extra_edge: float = Field(
        default=0.003,
        ge=0.0,
        le=0.2,
        validation_alias=AliasChoices("TRADE_EDGE_MIDDLE_EXTRA_EDGE", "trade_edge_middle_extra_edge"),
        description="Extra edge required near 50¢ mid. 0 = off; small values keep some caution at mid.",
    )
    trade_llm_min_net_edge_after_fees: float | None = Field(
        default=None,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_LLM_MIN_NET_EDGE_AFTER_FEES",
            "trade_llm_min_net_edge_after_fees",
        ),
        description="If set, llm-trade uses this min net edge instead of TRADE_MIN_NET_EDGE_AFTER_FEES (looser = more fills).",
    )
    trade_llm_edge_middle_extra_edge: float | None = Field(
        default=0.0,
        ge=0.0,
        le=0.2,
        validation_alias=AliasChoices(
            "TRADE_LLM_EDGE_MIDDLE_EXTRA_EDGE",
            "trade_llm_edge_middle_extra_edge",
        ),
        description="Extra mid-price edge for llm-trade only. Default 0 = no extra mid hurdle for LLM path (more fills). Set null via env omission to inherit TRADE_EDGE_MIDDLE_EXTRA_EDGE.",
    )
    trade_llm_screen_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("TRADE_LLM_SCREEN_ENABLED", "trade_llm_screen_enabled"),
    )
    trade_llm_model: str = Field(
        default="gpt-5.4-mini",
        validation_alias=AliasChoices("TRADE_LLM_MODEL", "trade_llm_model"),
        description=(
            "Chat Completions model for llm-trade / discover. Small GPT-5-class: gpt-5.4-mini (default), "
            "gpt-5.4-nano (cheapest), or legacy gpt-5-nano. Fallback: gpt-4o-mini if your key lacks GPT-5 family access."
        ),
    )
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "openai_api_key"),
    )
    sql_connection_string: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SQL_CONNECTION_STRING", "sql_connection_string"),
    )
    trade_llm_auto_execute: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_LLM_AUTO_EXECUTE",
            "trade_llm_auto_execute",
        ),
    )
    trade_llm_cli_execute: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_LLM_CLI_EXECUTE",
            "trade_llm_cli_execute",
        ),
        description="If true, llm-trade runs with execute=True without passing --execute (still needs TRADE_LLM_AUTO_EXECUTE; DRY_RUN respected).",
    )
    trade_llm_use_tape_universe: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_LLM_USE_TAPE_UNIVERSE",
            "trade_llm_use_tape_universe",
        ),
        description="If true, llm-trade ranks markets by recent public trade $ flow (same as --tape). Use with KALSHI_ENV=prod and TRADE_TAPE_* for active/live-style liquidity.",
    )
    trade_llm_max_markets_per_run: int = Field(
        default=900,
        ge=1,
        le=2000,
        validation_alias=AliasChoices(
            "TRADE_LLM_MAX_MARKETS_PER_RUN",
            "trade_llm_max_markets_per_run",
        ),
        description="How many distinct open-market tickers llm-trade considers per pass (paginated + deduped).",
    )
    trade_llm_open_markets_max_pages: int = Field(
        default=100,
        ge=1,
        le=200,
        validation_alias=AliasChoices(
            "TRADE_LLM_OPEN_MARKETS_MAX_PAGES",
            "trade_llm_open_markets_max_pages",
        ),
        description="llm-trade (open universe): max get_markets pages while collecting distinct tickers.",
    )
    trade_llm_random_skip_pages_max: int = Field(
        default=35,
        ge=0,
        le=120,
        validation_alias=AliasChoices(
            "TRADE_LLM_RANDOM_SKIP_PAGES_MAX",
            "trade_llm_random_skip_pages_max",
        ),
        description="Each llm-trade open-universe run skips 0..N list_markets pages before sampling so tickers rotate across runs.",
    )
    trade_llm_bitcoin_priority_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_LLM_BITCOIN_PRIORITY_ENABLED",
            "trade_llm_bitcoin_priority_enabled",
        ),
        description="Prepend open Bitcoin-series markets (by ticker prefix) before the general open-market walk.",
    )
    trade_llm_bitcoin_priority_prefix: str = Field(
        default="KXBTC",
        validation_alias=AliasChoices(
            "TRADE_LLM_BITCOIN_PRIORITY_PREFIX",
            "trade_llm_bitcoin_priority_prefix",
        ),
        description="Ticker prefix for Bitcoin contracts to prioritize in llm-trade (volume-sorted within prefix).",
    )
    trade_llm_bitcoin_priority_max_markets: int = Field(
        default=150,
        ge=0,
        le=500,
        validation_alias=AliasChoices(
            "TRADE_LLM_BITCOIN_PRIORITY_MAX_MARKETS",
            "trade_llm_bitcoin_priority_max_markets",
        ),
        description="Max BTC-prefix markets to merge at the front of the llm-trade list (0 = disable merge despite TRADE_LLM_BITCOIN_PRIORITY_ENABLED).",
    )
    trade_llm_shuffle_open_markets: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_LLM_SHUFFLE_OPEN_MARKETS",
            "trade_llm_shuffle_open_markets",
        ),
        description="If true, randomize order of open-market rows each run so the same API ordering does not always star the same tickers.",
    )
    trade_llm_crypto_fallback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_LLM_CRYPTO_FALLBACK_ENABLED",
            "trade_llm_crypto_fallback_enabled",
        ),
        description=(
            "When the primary open-universe pass submits no orders, run a second pass over all open markets "
            "matching TRADE_CRYPTO_KALSHI_PREFIXES (or legacy TRADE_BITCOIN_TICKER_PREFIX / default KXBTC), "
            "excluding tickers already scanned in the first pass."
        ),
    )
    trade_llm_crypto_fallback_max_markets: int = Field(
        default=2000,
        ge=1,
        le=2000,
        validation_alias=AliasChoices(
            "TRADE_LLM_CRYPTO_FALLBACK_MAX_MARKETS",
            "trade_llm_crypto_fallback_max_markets",
        ),
        description="Max distinct crypto-prefix markets to consider in the llm-trade fallback pass (union of prefixes, volume-sorted).",
    )
    trade_llm_crypto_fallback_max_pages: int = Field(
        default=100,
        ge=1,
        le=200,
        validation_alias=AliasChoices(
            "TRADE_LLM_CRYPTO_FALLBACK_MAX_PAGES",
            "trade_llm_crypto_fallback_max_pages",
        ),
        description="Per-prefix page cap while fetching open markets for llm-trade crypto fallback (same semantics as bitcoin discovery).",
    )
    trade_llm_merge_crypto_watch_signals: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_LLM_MERGE_CRYPTO_WATCH_SIGNALS",
            "trade_llm_merge_crypto_watch_signals",
        ),
        description=(
            "When true, ``llm-trade`` prepends tickers from the crypto-watch JSON file (written by ``kalshi-bot crypto-watch``) "
            "so the active trader prioritizes the same fee-edge candidates."
        ),
    )
    trade_llm_merge_ws_ticker_scan_signals: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_LLM_MERGE_WS_TICKER_SCAN_SIGNALS",
            "trade_llm_merge_ws_ticker_scan_signals",
        ),
        description=(
            "When true, ``llm-trade`` also merges tickers from ``ws-ticker-scan`` JSON (``.kalshi_ws_ticker_scan.json``); "
            "same merge cap as ``trade_llm_crypto_watch_merge_max``, deduped with crypto-watch by ticker (higher net_edge wins)."
        ),
    )
    trade_llm_crypto_watch_merge_max: int = Field(
        default=40,
        ge=0,
        le=500,
        validation_alias=AliasChoices(
            "TRADE_LLM_CRYPTO_WATCH_MERGE_MAX",
            "trade_llm_crypto_watch_merge_max",
        ),
        description="Max tickers to prepend from the crypto-watch file into each llm-trade open-universe pass (0 = disable merge).",
    )
    crypto_watch_state_path: str = Field(
        default="",
        validation_alias=AliasChoices(
            "CRYPTO_WATCH_STATE_PATH",
            "crypto_watch_state_path",
        ),
        description="Path to crypto-watch JSON (default: project root ``.kalshi_crypto_watch.json``). Shared with ``llm-trade`` merge.",
    )
    crypto_watch_max_markets_scan: int = Field(
        default=500,
        ge=1,
        le=2000,
        validation_alias=AliasChoices(
            "CRYPTO_WATCH_MAX_MARKETS_SCAN",
            "crypto_watch_max_markets_scan",
        ),
        description="Max distinct crypto-prefix markets to evaluate per crypto-watch iteration.",
    )
    crypto_watch_max_pages: int = Field(
        default=80,
        ge=1,
        le=200,
        validation_alias=AliasChoices(
            "CRYPTO_WATCH_MAX_PAGES",
            "crypto_watch_max_pages",
        ),
        description="Per-prefix API page cap for crypto-watch discovery.",
    )
    crypto_watch_min_net_edge_after_fees: float | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CRYPTO_WATCH_MIN_NET_EDGE_AFTER_FEES",
            "crypto_watch_min_net_edge_after_fees",
        ),
        description="Minimum fee-aware edge (YES, mid-as-fair) for crypto-watch pings; None = use TRADE_MIN_NET_EDGE_AFTER_FEES.",
    )
    dashboard_ingest_crypto_watch: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "DASHBOARD_INGEST_CRYPTO_WATCH",
            "dashboard_ingest_crypto_watch",
        ),
        description="If true, crypto-watch POSTs its JSON snapshot to the local dashboard ``/api/ingest_crypto_watch``.",
    )
    trade_ws_scan_state_path: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRADE_WS_SCAN_STATE_PATH",
            "trade_ws_scan_state_path",
        ),
        description="Path for ``ws-ticker-scan`` JSON (default: project ``.kalshi_ws_ticker_scan.json``).",
    )
    trade_ws_scan_ticker_prefixes: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRADE_WS_SCAN_TICKER_PREFIXES",
            "trade_ws_scan_ticker_prefixes",
        ),
        description="Comma-separated Kalshi ticker prefixes to score on the WS ticker stream; empty = same as ``TRADE_CRYPTO_KALSHI_PREFIXES`` discovery list.",
    )
    trade_ws_scan_min_seconds_between_same_ticker: float = Field(
        default=2.0,
        ge=0.0,
        le=3600.0,
        validation_alias=AliasChoices(
            "TRADE_WS_SCAN_MIN_SECONDS_BETWEEN_SAME_TICKER",
            "trade_ws_scan_min_seconds_between_same_ticker",
        ),
        description="Debounce: min seconds between re-evaluating the same market ticker on the WebSocket ticker channel.",
    )
    trade_ws_scan_flush_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=600.0,
        validation_alias=AliasChoices(
            "TRADE_WS_SCAN_FLUSH_SECONDS",
            "trade_ws_scan_flush_seconds",
        ),
        description="Minimum seconds between writing the WS scan JSON file to disk (and dashboard POST).",
    )
    trade_ws_scan_max_opportunities_in_file: int = Field(
        default=200,
        ge=1,
        le=5000,
        validation_alias=AliasChoices(
            "TRADE_WS_SCAN_MAX_OPPORTUNITIES_IN_FILE",
            "trade_ws_scan_max_opportunities_in_file",
        ),
        description="Cap rows kept in the WS ticker-scan state file (best net_edge per ticker already enforced).",
    )
    trade_ws_scan_respect_min_volume: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_WS_SCAN_RESPECT_MIN_VOLUME",
            "trade_ws_scan_respect_min_volume",
        ),
        description="If true, apply ``TRADE_MIN_MARKET_VOLUME`` using ticker message volume when present (WS volume may lag REST).",
    )
    trade_ws_scan_emit_dashboard_ping: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_WS_SCAN_EMIT_DASHBOARD_PING",
            "trade_ws_scan_emit_dashboard_ping",
        ),
        description="If true, emit ``crypto_watch_ping``-style monitor events when WS scan flushes with ≥1 opportunity.",
    )
    trade_ws_scan_use_rest_orderbook: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_WS_SCAN_USE_REST_ORDERBOOK",
            "trade_ws_scan_use_rest_orderbook",
        ),
        description=(
            "When true, ``ws-ticker-scan`` re-evaluates each debounced ticker with REST orderbook + the same "
            "``evaluate_crypto_yes_opportunity`` math as ``crypto-watch`` (implied YES ask from NO bid). "
            "When false, uses only WS ``yes_bid_dollars``/``yes_ask_dollars`` (mid−ask is usually negative; expect few signals)."
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
        description="If set, skip when (YES_ask − YES_bid) exceeds this. None = no maximum spread filter (only min_spread if >0).",
    )
    trade_llm_relaxed_approval: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_LLM_RELAXED_APPROVAL",
            "trade_llm_relaxed_approval",
        ),
        description="If true, more permissive LLM prompt (prefer approve on fair value / small edge). If false, stricter wording.",
    )
    trade_llm_accept_when_fair_covers_ask: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_LLM_ACCEPT_WHEN_FAIR_COVERS_ASK",
            "trade_llm_accept_when_fair_covers_ask",
        ),
        description="If true, when LLM declines but fair_yes is within slippage of ask, still run deterministic edge checks. false = respect LLM decline (stricter).",
    )
    trade_llm_fair_ask_slippage: float = Field(
        default=0.04,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_LLM_FAIR_ASK_SLIPPAGE",
            "trade_llm_fair_ask_slippage",
        ),
        description="Override LLM decline when fair_yes >= implied_YES_ask − this (e.g. 0.04 = 4¢).",
    )
    trade_llm_adapt_to_session_wl: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_LLM_ADAPT_TO_SESSION_WL",
            "trade_llm_adapt_to_session_wl",
        ),
        description="When true and session W–L skews negative, tighten fee-edge math and LLM prompt. Default false so a losing streak does not keep blocking new trades.",
    )
    trade_llm_adapt_min_closed_trades: int = Field(
        default=5,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices(
            "TRADE_LLM_ADAPT_MIN_CLOSED_TRADES",
            "trade_llm_adapt_min_closed_trades",
        ),
        description="Minimum wins+losses before W–L adaptation activates.",
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
        default=200,
        ge=1,
        le=2000,
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
    trade_bitcoin_kalshi_ticker: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRADE_BITCOIN_KALSHI_TICKER",
            "trade_bitcoin_kalshi_ticker",
        ),
        description="Optional: pin one Kalshi BTC contract. If empty, bitcoin-trade / sidecar discovers open markets whose ticker starts with TRADE_BITCOIN_TICKER_PREFIX (contracts roll often).",
    )
    trade_bitcoin_ticker_prefix: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRADE_BITCOIN_TICKER_PREFIX",
            "trade_bitcoin_ticker_prefix",
        ),
        description="When TRADE_BITCOIN_KALSHI_TICKER is empty: only open markets with tickers starting with this prefix (e.g. KXBTC for Bitcoin series).",
    )
    trade_bitcoin_max_universe: int = Field(
        default=80,
        ge=1,
        le=500,
        validation_alias=AliasChoices(
            "TRADE_BITCOIN_MAX_UNIVERSE",
            "trade_bitcoin_max_universe",
        ),
        description="Max open BTC contracts to collect when discovering by prefix (sorted by volume desc).",
    )
    trade_bitcoin_discovery_max_pages: int = Field(
        default=40,
        ge=1,
        le=200,
        validation_alias=AliasChoices(
            "TRADE_BITCOIN_DISCOVERY_MAX_PAGES",
            "trade_bitcoin_discovery_max_pages",
        ),
        description="Safety cap on get_markets pages while scanning for TRADE_BITCOIN_TICKER_PREFIX matches.",
    )
    trade_bitcoin_auto_execute: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_BITCOIN_AUTO_EXECUTE",
            "trade_bitcoin_auto_execute",
        ),
        description="If true, bitcoin-trade may submit when --execute (still needs LIVE_TRADING and not DRY_RUN).",
    )
    trade_bitcoin_sidecar_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_BITCOIN_SIDECAR_ENABLED",
            "trade_bitcoin_sidecar_enabled",
        ),
        description="During tape-trade / discover-trade, run Bitcoin logic every N ticker scans (pinned ticker or rotating prefix universe; shared counters across loop iterations).",
    )
    trade_bitcoin_every_n_ticker_scans: int = Field(
        default=50,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices(
            "TRADE_BITCOIN_EVERY_N_TICKER_SCANS",
            "trade_bitcoin_every_n_ticker_scans",
        ),
        description="With TRADE_BITCOIN_SIDECAR_ENABLED: run Bitcoin Kalshi check every N tape/discover ticker iterations.",
    )
    trade_crypto_kalshi_prefixes: str = Field(
        default="",
        validation_alias=AliasChoices(
            "TRADE_CRYPTO_KALSHI_PREFIXES",
            "trade_crypto_kalshi_prefixes",
        ),
        description=(
            "Comma-separated Kalshi ticker prefixes for crypto ``bitcoin-trade`` discovery (e.g. KXBTC,KXETH). "
            "When empty, TRADE_BITCOIN_TICKER_PREFIX is used if set, otherwise KXBTC. "
            "TRADE_BITCOIN_KALSHI_TICKER still pins a single market when set."
        ),
    )
    trade_crypto_spot_price_source: str = Field(
        default="auto",
        validation_alias=AliasChoices(
            "TRADE_CRYPTO_SPOT_PRICE_SOURCE",
            "trade_crypto_spot_price_source",
        ),
        description=(
            "Reference spot USD for logs: auto (try CoinGecko then Binance), coingecko, or binance. "
            "BTC vs ETH is inferred from the contract ticker (KXETH → ETH)."
        ),
    )

    # Prior-chart momentum (REST candlesticks): buy YES when YES trade price rose quickly in recent bars.
    trade_momentum_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("TRADE_MOMENTUM_ENABLED", "trade_momentum_enabled"),
    )
    trade_momentum_period_minutes: int = Field(
        default=5,
        ge=1,
        le=1440,
        validation_alias=AliasChoices("TRADE_MOMENTUM_PERIOD_MINUTES", "trade_momentum_period_minutes"),
        description="Candlestick interval for Kalshi batch_get_market_candlesticks.",
    )
    trade_momentum_lookback_minutes: int = Field(
        default=120,
        ge=5,
        le=10080,
        validation_alias=AliasChoices("TRADE_MOMENTUM_LOOKBACK_MINUTES", "trade_momentum_lookback_minutes"),
        description="How far back to request candlesticks (converted to seconds for the API).",
    )
    trade_momentum_min_candles: int = Field(
        default=4,
        ge=2,
        le=500,
        validation_alias=AliasChoices("TRADE_MOMENTUM_MIN_CANDLES", "trade_momentum_min_candles"),
        description="Minimum bars with a non-null trade close before evaluating momentum.",
    )
    trade_momentum_short_candles: int = Field(
        default=6,
        ge=2,
        le=100,
        validation_alias=AliasChoices("TRADE_MOMENTUM_SHORT_CANDLES", "trade_momentum_short_candles"),
        description="How many recent bars define the 'fast move' (capped by available closes).",
    )
    trade_momentum_min_net_rise_dollars: float = Field(
        default=0.015,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "TRADE_MOMENTUM_MIN_NET_RISE_DOLLARS",
            "trade_momentum_min_net_rise_dollars",
        ),
        description="Min YES price rise (0–1 scale on $1) over the short window, e.g. 0.015 = 1.5¢.",
    )
    trade_momentum_min_rise_per_candle_dollars: float = Field(
        default=0.002,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "TRADE_MOMENTUM_MIN_RISE_PER_CANDLE_DOLLARS",
            "trade_momentum_min_rise_per_candle_dollars",
        ),
        description="Min average rise per candle in the short window (quick move vs slow drift).",
    )
    trade_momentum_llm_bypass: bool = Field(
        default=False,
        validation_alias=AliasChoices("TRADE_MOMENTUM_LLM_BYPASS", "trade_momentum_llm_bypass"),
        description="If true, llm-trade submits on hot momentum before calling the LLM (tape/open scan).",
    )

    # Balance-scaled limits (bigger account → larger caps within fixed % of balance)
    trade_balance_sizing_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_BALANCE_SIZING_ENABLED",
            "trade_balance_sizing_enabled",
        ),
        description="Scale exposure, per-order contracts, and per-order notional from account balance (percent fields below). Static MAX_* values apply when this is false or balance is unavailable.",
    )
    trade_risk_pct_of_balance_per_trade: float = Field(
        default=0.04,
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
        description="With balance sizing: max total exposure ≈ balance×this (ignored when TRADE_NO_MAX_EXPOSURE_CAP=true).",
    )
    trade_no_max_exposure_cap: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_NO_MAX_EXPOSURE_CAP",
            "trade_no_max_exposure_cap",
        ),
        description="If true, do not cap total portfolio exposure by balance×TRADE_TOTAL_RISK_PCT (uses unlimited cap while balance>0). New orders still blocked when API reports balance≤0. Per-order sizing still uses TRADE_RISK_PCT_OF_BALANCE_PER_TRADE. If balance is unknown, falls back to MAX_EXPOSURE_CENTS.",
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
    trade_auto_sell_after_each_pass: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_AUTO_SELL_AFTER_EACH_PASS",
            "trade_auto_sell_after_each_pass",
        ),
        description=(
            "After each llm-trade / discover-trade / tape-trade / bitcoin-trade pass, scan long YES positions and run "
            "take-profit (same rules as auto-sell). Set false if you run a dedicated `kalshi-bot sell-bot` (or "
            "`exit-scan --loop --execute`) in another process so the trade bot only scans and submits entries."
        ),
    )
    trade_position_watch_before_auto_sell: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_POSITION_WATCH_BEFORE_AUTO_SELL",
            "trade_position_watch_before_auto_sell",
        ),
        description=(
            "When TRADE_AUTO_SELL_AFTER_EACH_PASS runs, print a short positions-watch table (book + tape lean) "
            "before the exit scan — same data as `positions-watch` without a second terminal."
        ),
    )
    sell_bot_interval_seconds: float = Field(
        default=30.0,
        ge=5.0,
        validation_alias=AliasChoices(
            "SELL_BOT_INTERVAL_SECONDS",
            "sell_bot_interval_seconds",
        ),
        description=(
            "Default seconds between `sell-bot` loop iterations (parallel exit-only process). "
            "Override with `kalshi-bot sell-bot --interval SEC`."
        ),
    )
    trade_exit_tape_no_heavy_relax_min_profit_cents: float = Field(
        default=0.0,
        ge=0.0,
        le=50.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TAPE_NO_HEAVY_RELAX_MIN_PROFIT_CENTS",
            "trade_exit_tape_no_heavy_relax_min_profit_cents",
        ),
        description=(
            "Auto-sell / exit-scan: if recent public tape for that ticker is NO-heavy (taker YES share ≤ "
            "TRADE_EXIT_TAPE_NO_HEAVY_MAX_YES_SHARE) and enough prints exist, reduce required min-profit ¢ by this "
            "amount for take-profit only (0 = off). Helps exit when flow turns against YES."
        ),
    )
    trade_exit_tape_no_heavy_max_yes_share: float = Field(
        default=0.38,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TAPE_NO_HEAVY_MAX_YES_SHARE",
            "trade_exit_tape_no_heavy_max_yes_share",
        ),
        description="Tape is NO-heavy when taker YES notional share ≤ this (0.38 ≈ 38% on YES side).",
    )
    trade_exit_tape_min_trades_for_exit: int = Field(
        default=8,
        ge=1,
        le=500,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TAPE_MIN_TRADES_FOR_EXIT",
            "trade_exit_tape_min_trades_for_exit",
        ),
        description="Minimum parsed taker prints on this ticker before tape relax can apply.",
    )
    trade_exit_tape_lookback_max_trades: int = Field(
        default=150,
        ge=10,
        le=1000,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TAPE_LOOKBACK_MAX_TRADES",
            "trade_exit_tape_lookback_max_trades",
        ),
        description="How many recent public trades to fetch per ticker for tape lean during auto-sell.",
    )
    trade_exit_hold_to_settlement_min_chance_cents: int = Field(
        default=90,
        ge=0,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_EXIT_HOLD_TO_SETTLEMENT_MIN_CHANCE_CENTS",
            "trade_exit_hold_to_settlement_min_chance_cents",
        ),
        description=(
            "If >0: while long YES and implied YES chance (mid of best bid and lift YES ask, 1–99¢) ≥ this, "
            "do not auto-sell—hold for final payout. 0 disables (always allow TP/stops per other rules)."
        ),
    )
    trade_exit_sell_within_cents_of_max_payout: int = Field(
        default=2,
        ge=0,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_EXIT_SELL_WITHIN_CENTS_OF_MAX_PAYOUT",
            "trade_exit_sell_within_cents_of_max_payout",
        ),
        description=(
            "If >0: when implied YES chance is within this many cents of 100¢ (i.e. chance ≥ 100 − N), "
            "do not apply hold-to-settlement—allow exits. If other exit rules do not fire, still sell "
            "(take-profit near max). 0 disables. Takes precedence over "
            "TRADE_EXIT_HOLD_TO_SETTLEMENT_MIN_CHANCE_CENTS when both would apply."
        ),
    )
    trade_exit_min_profit_cents_when_no_full_payout_indication: int = Field(
        default=5,
        ge=0,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_EXIT_MIN_PROFIT_CENTS_NO_FULL_PAYOUT_INDICATION",
            "trade_exit_min_profit_cents_when_no_full_payout_indication",
        ),
        description=(
            "If >0 and TRADE_EXIT_FULL_PAYOUT_INDICATION_MIN_CHANCE_CENTS >0: when implied YES chance is **below** "
            "that threshold (no strong indication the contract resolves YES at $1), take profit when best bid ≥ "
            "entry + this many ¢ per share. 0 disables (use normal TRADE_EXIT_MIN_PROFIT_* only)."
        ),
    )
    trade_exit_full_payout_indication_min_chance_cents: int = Field(
        default=90,
        ge=0,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_EXIT_FULL_PAYOUT_INDICATION_MIN_CHANCE_CENTS",
            "trade_exit_full_payout_indication_min_chance_cents",
        ),
        description=(
            "If >0: implied YES chance ≥ this counts as an indication of winning the full $1 payout for exit math—"
            "the relaxed min-profit rule above is not used; normal min-profit rules apply. If 0: relaxed rule off."
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
        description="When implied-% exits are on (TRADE_EXIT_ONLY_PROFIT_MARGIN=false): min best YES bid (1–99) to count as take-profit.",
    )
    trade_exit_min_profit_cents_per_contract: float | None = Field(
        default=15.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_MIN_PROFIT_CENTS_PER_CONTRACT",
            "TRADE_EXIT_MIN_PROFIT_CENTS",
            "trade_exit_min_profit_cents_per_contract",
        ),
        description=(
            "Floor: min profit vs entry (¢ per share). Combined with TRADE_EXIT_MIN_PROFIT_PCT_OF_ENTRY: "
            "effective = max(this, entry×pct), then optional cap."
        ),
    )
    trade_exit_min_profit_pct_of_entry: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_MIN_PROFIT_PCT_OF_ENTRY",
            "trade_exit_min_profit_pct_of_entry",
        ),
        description=(
            "If >0: require at least this fraction of entry (in ¢) as profit, e.g. 0.25 → 25% of entry. "
            "Effective min profit = max(TRADE_EXIT_MIN_PROFIT_CENTS_PER_CONTRACT, entry×this). 0 = floor only."
        ),
    )
    trade_exit_take_profit_min_bid_vs_entry_multiplier: float = Field(
        default=1.5,
        ge=0.0,
        le=3.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TAKE_PROFIT_MIN_BID_VS_ENTRY_MULTIPLIER",
            "trade_exit_take_profit_min_bid_vs_entry_multiplier",
        ),
        description=(
            "When >1.0: take-profit when best YES bid ≥ ceil(entry×this), e.g. 1.5 with entry 50¢ → exit at bid ≥75¢ "
            "(+50% vs entry in ¢ terms). This path also bypasses hold-to-settlement. 0 or 1.0 = off."
        ),
    )
    trade_exit_min_profit_cents_cap: float | None = Field(
        default=25.0,
        ge=0.0,
        le=99.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_MIN_PROFIT_CENTS_CAP",
            "trade_exit_min_profit_cents_cap",
        ),
        description="Optional ceiling (¢) on required min profit after floor/pct (None = no cap). Default 25.",
    )
    trade_exit_lock_profit_cents: float | None = Field(
        default=15.0,
        ge=0.0,
        le=99.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_LOCK_PROFIT_CENTS",
            "trade_exit_lock_profit_cents",
        ),
        description=(
            "Once session peak best bid reaches entry + this many ¢, the exit floor includes at least entry + this "
            "(locks that profit if price falls back). Combined with trailing: max(fixed, trail, entry+lock). "
            "Set 0 or omit via env empty to disable."
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
        default=True,
        validation_alias=AliasChoices(
            "TRADE_EXIT_ONLY_PROFIT_MARGIN",
            "trade_exit_only_profit_margin",
        ),
        description="If true, skip implied-% TP; exit when bid ≥ entry + effective min profit (floor / pct / cap).",
    )
    trade_exit_stop_loss_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_EXIT_STOP_LOSS_ENABLED",
            "trade_exit_stop_loss_enabled",
        ),
        description="If true, sell long YES when best bid ≤ entry × TRADE_EXIT_STOP_LOSS_ENTRY_FRACTION (requires entry reference).",
    )
    trade_exit_stop_loss_entry_fraction: float = Field(
        default=0.8,
        gt=0.0,
        lt=1.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_STOP_LOSS_ENTRY_FRACTION",
            "trade_exit_stop_loss_entry_fraction",
        ),
        description=(
            "Long YES stop: fire when best YES bid ≤ round(entry_cents × this). "
            "0.8 = exit if bid is at or below 80% of estimated entry in ¢ (e.g. 50¢ entry → floor 40¢). "
            "Higher fraction = tighter stop (cuts losses sooner)."
        ),
    )
    trade_exit_stop_loss_skip_suspect_portfolio_estimate: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_EXIT_STOP_LOSS_SKIP_SUSPECT_PORTFOLIO_ESTIMATE",
            "trade_exit_stop_loss_skip_suspect_portfolio_estimate",
        ),
        description=(
            "If true, do not apply stop-loss when entry comes only from portfolio (total_traded/position) "
            "and rounds to ≤5¢ or ≥95¢—often API noise. Manual TRADE_EXIT_ENTRY_REFERENCE_YES_CENTS is always used for stops. "
            "Take-profit rules still use the portfolio estimate."
        ),
    )
    trade_exit_hedge_loser_stop_boost_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_EXIT_HEDGE_LOSER_STOP_BOOST_ENABLED",
            "trade_exit_hedge_loser_stop_boost_enabled",
        ),
        description=(
            "If true: when you hold long YES on two or more markets in the same Kalshi event (mutually exclusive "
            "outcomes), the leg with the worst mark (best bid minus entry) uses a higher stop floor "
            "(TRADE_EXIT_STOP_LOSS_ENTRY_FRACTION + TRADE_EXIT_HEDGE_LOSER_STOP_LOSS_FRACTION_ADD, capped). "
            "A higher fraction means a higher minimum bid before the stop fires—cuts the loser sooner."
        ),
    )
    trade_exit_hedge_loser_stop_loss_fraction_add: float = Field(
        default=0.12,
        ge=0.0,
        le=0.45,
        validation_alias=AliasChoices(
            "TRADE_EXIT_HEDGE_LOSER_STOP_LOSS_FRACTION_ADD",
            "trade_exit_hedge_loser_stop_loss_fraction_add",
        ),
        description=(
            "Added to TRADE_EXIT_STOP_LOSS_ENTRY_FRACTION for the worst same-event hedge leg only "
            "(when TRADE_EXIT_HEDGE_LOSER_STOP_BOOST_ENABLED). Capped so effective fraction stays below 1."
        ),
    )
    trade_scan_down_pct_sell: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices(
            "TRADE_SCAN_DOWN_PCT_SELL",
            "trade_scan_down_pct_sell",
        ),
        description=(
            "If > 0: automatically sell a long YES position during each scan pass when the best YES bid "
            "has fallen this many percent or more below the estimated entry price "
            "(e.g. 20 = sell when bid ≤ entry − 20% of entry). "
            "0 = disabled. Works independently of TRADE_EXIT_STOP_LOSS_ENABLED and its fraction."
        ),
    )
    trade_exit_trailing_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TRAILING_ENABLED",
            "trade_exit_trailing_enabled",
        ),
        description=(
            "Track best YES bid peak per ticker; after price trends up vs entry, exit on pullback from peak "
            "and optionally raise the fixed stop floor (see TRADE_EXIT_TRAILING_*). Take-profit still runs first."
        ),
    )
    trade_exit_trailing_pullback_cents: float = Field(
        default=4.0,
        ge=0.0,
        le=50.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TRAILING_PULLBACK_CENTS",
            "trade_exit_trailing_pullback_cents",
        ),
        description="Sell when best bid drops this many ¢ below the session peak (after trailing arms).",
    )
    trade_exit_trailing_pullback_pct_of_peak: float = Field(
        default=0.0,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TRAILING_PULLBACK_PCT_OF_PEAK",
            "trade_exit_trailing_pullback_pct_of_peak",
        ),
        description="If >0: pullback distance is max(cents, peak×this). 0 = use cents only.",
    )
    trade_exit_trailing_activate_above_entry_cents: int = Field(
        default=1,
        ge=0,
        le=50,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TRAILING_ACTIVATE_ABOVE_ENTRY_CENTS",
            "trade_exit_trailing_activate_above_entry_cents",
        ),
        description="Require peak ≥ entry + this before trailing / raised stop applies (avoids arming on noise).",
    )
    trade_exit_trailing_combine_with_fixed_stop: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TRAILING_COMBINE_WITH_FIXED_STOP",
            "trade_exit_trailing_combine_with_fixed_stop",
        ),
        description=(
            "If true: combine peak−pullback with a fraction stop. When TRADE_EXIT_TRAILING_BID_FRACTION_CAPS_PEAK_TRAIL "
            "is true (default), exit threshold is min(peak−pullback, best_bid×fraction) so the level moves **down** "
            "when price falls; if false, legacy max(entry×fraction, peak−pullback)."
        ),
    )
    trade_exit_trailing_bid_fraction_caps_peak_trail: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TRAILING_BID_FRACTION_CAPS_PEAK_TRAIL",
            "trade_exit_trailing_bid_fraction_caps_peak_trail",
        ),
        description=(
            "When true and trailing is combined with fixed stop: exit when bid ≤ min(peak−pullback, "
            "round(best_bid×effective_fraction)) (then profit-lock floor). Lets the stop **decrease** as the "
            "market trades lower (fraction of current bid), while still using peak−pullback on the way up. "
            "If false: legacy max(entry×fraction, peak−pullback)."
        ),
    )
    trade_exit_trailing_stop_loss_floor_fraction: float | None = Field(
        default=None,
        gt=0.0,
        lt=1.0,
        validation_alias=AliasChoices(
            "TRADE_EXIT_TRAILING_STOP_LOSS_FLOOR_FRACTION",
            "trade_exit_trailing_stop_loss_floor_fraction",
        ),
        description=(
            "When trailing is armed: fixed stop uses max(TRADE_EXIT_STOP_LOSS_ENTRY_FRACTION, this) vs entry. "
            "Raises the stop floor on winners (e.g. 0.58 vs 0.50). Omit to use only TRADE_EXIT_STOP_LOSS_ENTRY_FRACTION."
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
    trade_rebuy_after_stop_loss_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_REBUY_AFTER_STOP_LOSS_ENABLED",
            "trade_rebuy_after_stop_loss_enabled",
        ),
        description=(
            "If true: after a fixed stop-loss sell (and optionally trailing/profit-lock), submit a new buy YES "
            "on the same ticker using TRADE_BUY_LIMIT_YES_PRICE_CENTS / TRADE_BUY_MAX_YES_ASK_DOLLARS gates."
        ),
    )
    trade_rebuy_after_stop_loss_include_trailing_and_profit_lock: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_REBUY_AFTER_STOP_LOSS_INCLUDE_TRAILING_AND_PROFIT_LOCK",
            "trade_rebuy_after_stop_loss_include_trailing_and_profit_lock",
        ),
        description="If true: also re-enter after trailing_stop_pullback or profit_lock_stop (not only stop_loss_entry_fraction).",
    )
    trade_rebuy_after_stop_loss_delay_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=120.0,
        validation_alias=AliasChoices(
            "TRADE_REBUY_AFTER_STOP_LOSS_DELAY_SECONDS",
            "trade_rebuy_after_stop_loss_delay_seconds",
        ),
        description="Sleep this long after the stop sell before placing the rebuy (lets the book update).",
    )
    trade_rebuy_after_stop_loss_cooldown_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=3600.0,
        validation_alias=AliasChoices(
            "TRADE_REBUY_AFTER_STOP_LOSS_COOLDOWN_SECONDS",
            "trade_rebuy_after_stop_loss_cooldown_seconds",
        ),
        description="Minimum seconds between stop-loss rebuys on the same ticker (0 = no cooldown).",
    )
    trade_rebuy_after_stop_loss_time_in_force: Literal["immediate_or_cancel", "fill_or_kill", "good_till_canceled"] = Field(
        default="immediate_or_cancel",
        validation_alias=AliasChoices(
            "TRADE_REBUY_AFTER_STOP_LOSS_TIME_IN_FORCE",
            "trade_rebuy_after_stop_loss_time_in_force",
        ),
        description="Time-in-force for the rebuy limit order (IOC default avoids resting).",
    )
    trade_double_down_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_DOUBLE_DOWN_ENABLED",
            "trade_double_down_enabled",
        ),
        description=(
            "llm-trade only: if you already hold long YES on a ticker, allow another buy YES "
            "(up to TRADE_DOUBLE_DOWN_MAX_POSITION_CONTRACTS total) when the LLM and fee-edge still pass."
        ),
    )
    trade_double_down_max_position_contracts: int = Field(
        default=5,
        ge=2,
        le=99,
        validation_alias=AliasChoices(
            "TRADE_DOUBLE_DOWN_MAX_POSITION_CONTRACTS",
            "trade_double_down_max_position_contracts",
        ),
        description="Max total YES contracts per market when adding to a winner (must be ≥ max_contracts_per_market if you double down to the cap).",
    )
    trade_double_down_extra_min_net_edge_after_fees: float = Field(
        default=0.0,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_DOUBLE_DOWN_EXTRA_MIN_NET_EDGE_AFTER_FEES",
            "trade_double_down_extra_min_net_edge_after_fees",
        ),
        description="Extra fee-edge hurdle (0–1 on $1 face) on add-on buys only; 0 = same as normal llm-trade edge.",
    )
    trade_spike_fade_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "TRADE_SPIKE_FADE_ENABLED",
            "trade_spike_fade_enabled",
        ),
        description=(
            "llm-trade only: if recent YES candle closes show a sharp rise, tighten fee-edge for buy YES "
            "unless net edge clears TRADE_SPIKE_FADE_HUGE_NET_EDGE_AFTER_FEES (fade the spike unless mispricing is huge)."
        ),
    )
    trade_spike_fade_period_interval_minutes: int = Field(
        default=5,
        ge=1,
        le=1440,
        validation_alias=AliasChoices(
            "TRADE_SPIKE_FADE_PERIOD_INTERVAL_MINUTES",
            "trade_spike_fade_period_interval_minutes",
        ),
        description="Candle size (minutes) for spike detection (same REST endpoint as momentum).",
    )
    trade_spike_fade_lookback_minutes: int = Field(
        default=120,
        ge=5,
        le=10080,
        validation_alias=AliasChoices(
            "TRADE_SPIKE_FADE_LOOKBACK_MINUTES",
            "trade_spike_fade_lookback_minutes",
        ),
        description="How far back to fetch YES trade closes for spike detection.",
    )
    trade_spike_fade_min_candles: int = Field(
        default=4,
        ge=2,
        le=500,
        validation_alias=AliasChoices(
            "TRADE_SPIKE_FADE_MIN_CANDLES",
            "trade_spike_fade_min_candles",
        ),
        description="Minimum candles required before evaluating a spike.",
    )
    trade_spike_fade_short_candles: int = Field(
        default=6,
        ge=2,
        le=200,
        validation_alias=AliasChoices(
            "TRADE_SPIKE_FADE_SHORT_CANDLES",
            "trade_spike_fade_short_candles",
        ),
        description="Last N bars used to measure net YES rise (spike if rise >= min_net_rise).",
    )
    trade_spike_fade_min_net_rise_dollars: float = Field(
        default=0.025,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_SPIKE_FADE_MIN_NET_RISE_DOLLARS",
            "trade_spike_fade_min_net_rise_dollars",
        ),
        description="YES rise (dollars on $1 face) over the short window counts as a spike (e.g. 0.025 = 2.5¢).",
    )
    trade_spike_fade_extra_min_net_edge_after_fees: float = Field(
        default=0.03,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_SPIKE_FADE_EXTRA_MIN_NET_EDGE_AFTER_FEES",
            "trade_spike_fade_extra_min_net_edge_after_fees",
        ),
        description="Added to the usual min net edge when a spike is detected and net edge is below the huge bypass.",
    )
    trade_spike_fade_huge_net_edge_after_fees: float = Field(
        default=0.10,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_SPIKE_FADE_HUGE_NET_EDGE_AFTER_FEES",
            "trade_spike_fade_huge_net_edge_after_fees",
        ),
        description="If fee-adjusted net edge (per share) is at or above this, allow buy YES despite spike fade tightening.",
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
    dashboard_portfolio_poll_seconds: float = Field(
        default=20.0,
        ge=0.0,
        le=3600.0,
        validation_alias=_env("DASHBOARD_PORTFOLIO_POLL_SECONDS", "dashboard_portfolio_poll_seconds"),
        description="While llm-trade / tape-trade / discover-trade / bitcoin-trade run with the dashboard: poll balance & exposure this often (seconds). 0 = only record at start/end of each pipeline pass.",
    )
    dashboard_ingest_auto_sell: bool = Field(
        default=True,
        validation_alias=_env("DASHBOARD_INGEST_AUTO_SELL", "dashboard_ingest_auto_sell"),
        description=(
            "If true, after an auto-sell/exit-scan sell, POST the outcome to the local dashboard (127.0.0.1) "
            "so session W–L and the event table update when auto-sell runs in a separate terminal. Falls back to "
            "in-process recording if nothing is listening."
        ),
    )
    dashboard_ingest_trade_events: bool = Field(
        default=True,
        validation_alias=_env("DASHBOARD_INGEST_TRADE_EVENTS", "dashboard_ingest_trade_events"),
        description=(
            "If true, each order/trade dashboard event (dry-run, live submit, blocked, etc.) POSTs to the local "
            "dashboard first so the Trades & orders feed updates when trading runs in a different process than "
            "`--web`. On HTTP success the event is not duplicated in the sender's memory. Falls back to in-process "
            "if nothing is listening. Heartbeats stay in-process only."
        ),
    )
    dashboard_ingest_portfolio_series: bool = Field(
        default=True,
        validation_alias=_env("DASHBOARD_INGEST_PORTFOLIO_SERIES", "dashboard_ingest_portfolio_series"),
        description=(
            "If true, after each trading pass when the dashboard is not in-process (no --web), POST to the local "
            "dashboard so it appends a portfolio chart point immediately. Same machine must run a dashboard with "
            "API keys in .env for the snapshot fetch."
        ),
    )
    dashboard_ingest_pass_summary: bool = Field(
        default=True,
        validation_alias=_env("DASHBOARD_INGEST_PASS_SUMMARY", "dashboard_ingest_pass_summary"),
        description=(
            "If true, when llm/discover/tape/bitcoin-trade runs without --web, POST pass stats to the local dashboard "
            "so the UI can refresh Kalshi-heavy panels when each loop iteration finishes (not only on a slow timer)."
        ),
    )

    log_level: str = Field(default="INFO", validation_alias=_env("LOG_LEVEL", "log_level"))
    structured_log_path: Path = Field(
        default_factory=_default_log_path, validation_alias=_env("STRUCTURED_LOG_PATH", "structured_log_path")
    )
    structured_log_clear_every_n_tickers: int = Field(
        default=0,
        ge=0,
        validation_alias=_env("STRUCTURED_LOG_CLEAR_EVERY_N_TICKERS", "structured_log_clear_every_n_tickers"),
        description="Per-ticker: truncate STRUCTURED_LOG_PATH after every N ticker iterations inside one pass (0 = never). Prefer STRUCTURED_LOG_CLEAR_EVERY_OTHER_PASS when using --loop.",
    )
    structured_log_clear_every_other_pass: bool = Field(
        default=True,
        validation_alias=_env("STRUCTURED_LOG_CLEAR_EVERY_OTHER_PASS", "structured_log_clear_every_other_pass"),
        description="llm/discover/tape/bitcoin-trade with --loop: truncate JSONL after every 2nd completed pass (2, 4, 6…).",
    )
    structured_log_preserve_executed_on_flush: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "STRUCTURED_LOG_PRESERVE_EXECUTED_ON_FLUSH",
            "structured_log_preserve_executed_on_flush",
        ),
        description=(
            "When a structured-log flush runs (every-N tickers or every other pass), rewrite JSONL to keep only "
            "executed-bet lines (orders + auto-sell closes) instead of deleting the whole file."
        ),
    )
    trade_bet_history_edge_penalty_per_loss: float = Field(
        default=0.0,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_BET_HISTORY_EDGE_PENALTY_PER_LOSS",
            "trade_bet_history_edge_penalty_per_loss",
        ),
        description="Add this much to required min net edge per realized loss on the same ticker (from JSONL). 0 = off.",
    )
    trade_bet_history_max_edge_penalty: float = Field(
        default=0.06,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices(
            "TRADE_BET_HISTORY_MAX_EDGE_PENALTY",
            "trade_bet_history_max_edge_penalty",
        ),
        description="Cap on total extra min-edge from bet-history penalties per ticker.",
    )
    trade_bet_history_skip_ticker_min_losses: int = Field(
        default=0,
        ge=0,
        le=100,
        validation_alias=AliasChoices(
            "TRADE_BET_HISTORY_SKIP_TICKER_MIN_LOSSES",
            "trade_bet_history_skip_ticker_min_losses",
        ),
        description="If >0, skip new entries on tickers with at least this many logged realized losses. 0 = never skip.",
    )
    trade_bet_history_scan_max_bytes: int = Field(
        default=12_000_000,
        ge=100_000,
        le=200_000_000,
        validation_alias=AliasChoices(
            "TRADE_BET_HISTORY_SCAN_MAX_BYTES",
            "trade_bet_history_scan_max_bytes",
        ),
        description="Tail of STRUCTURED_LOG_PATH to scan for per-ticker win/loss stats.",
    )
    trade_bet_history_scan_max_lines: int = Field(
        default=120_000,
        ge=1_000,
        le=2_000_000,
        validation_alias=AliasChoices(
            "TRADE_BET_HISTORY_SCAN_MAX_LINES",
            "trade_bet_history_scan_max_lines",
        ),
        description="Max JSONL lines (from tail) to scan for bet-history outcomes.",
    )

    @field_validator("kalshi_rest_base_url", "kalshi_ws_url", mode="before")
    @classmethod
    def _blank_url_to_none(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    @field_validator("openai_api_key", "sql_connection_string", mode="before")
    @classmethod
    def _blank_str_to_none(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    @field_validator("trade_notional_sweep_usd", mode="before")
    @classmethod
    def _blank_notional_sweep_to_none(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    @field_validator("trade_crypto_spot_price_source", mode="before")
    @classmethod
    def _normalize_crypto_spot_source(cls, v: object) -> str:
        s = str(v or "auto").strip().lower()
        if s in ("auto", "coingecko", "binance"):
            return s
        return "auto"

    @field_validator("max_contracts_per_market", "strategy_order_count", mode="before")
    @classmethod
    def _clamp_order_share_counts(cls, v: object) -> object:
        """Clamp env integers to 1–99 (Kalshi contract counts)."""
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return v
        try:
            n = int(v)
        except (TypeError, ValueError):
            return v
        return min(99, max(1, n))

    @field_validator(
        "trade_exit_sell_time_in_force",
        "trade_rebuy_after_stop_loss_time_in_force",
        mode="before",
    )
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
                "time_in_force must be immediate_or_cancel, fill_or_kill, or good_till_canceled "
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
        "dashboard_ingest_auto_sell",
        "dashboard_ingest_trade_events",
        "dashboard_ingest_portfolio_series",
        "dashboard_ingest_pass_summary",
        "trade_use_edge_strategy",
        "trade_llm_screen_enabled",
        "trade_llm_auto_execute",
        "trade_llm_cli_execute",
        "trade_llm_use_tape_universe",
        "trade_llm_relaxed_approval",
        "trade_llm_accept_when_fair_covers_ask",
        "trade_llm_adapt_to_session_wl",
        "trade_llm_shuffle_open_markets",
        "trade_llm_bitcoin_priority_enabled",
        "trade_no_max_exposure_cap",
        "trade_discover_auto_execute",
        "trade_tape_auto_execute",
        "trade_bitcoin_auto_execute",
        "trade_bitcoin_sidecar_enabled",
        "trade_balance_sizing_enabled",
        "trade_auto_sell_after_each_pass",
        "trade_position_watch_before_auto_sell",
        "trade_exit_only_profit_margin",
        "trade_exit_estimate_entry_from_portfolio",
        "trade_exit_stop_loss_enabled",
        "trade_exit_stop_loss_skip_suspect_portfolio_estimate",
        "trade_exit_hedge_loser_stop_boost_enabled",
        "trade_exit_trailing_enabled",
        "trade_exit_trailing_combine_with_fixed_stop",
        "trade_exit_trailing_bid_fraction_caps_peak_trail",
        "trade_rebuy_after_stop_loss_enabled",
        "trade_rebuy_after_stop_loss_include_trailing_and_profit_lock",
        "trade_double_down_enabled",
        "trade_spike_fade_enabled",
        "structured_log_clear_every_other_pass",
        "structured_log_preserve_executed_on_flush",
        "trade_ws_scan_use_rest_orderbook",
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

    @model_validator(mode="after")
    def _theta_yes_ask_band(self) -> "Settings":
        if self.trade_entry_theta_min_yes_ask_cents > self.trade_entry_theta_max_yes_ask_cents:
            raise ValueError(
                "TRADE_ENTRY_THETA_MIN_YES_ASK_CENTS must be <= TRADE_ENTRY_THETA_MAX_YES_ASK_CENTS"
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
    def trade_entry_effective_max_yes_ask_dollars(self) -> float:
        """Min of ``strategy_max_yes_ask_dollars`` and ``trade_entry_hard_max_yes_ask_cents`` / 100 (actual entry ceiling)."""
        cap = float(self.trade_entry_hard_max_yes_ask_cents) / 100.0
        return min(float(self.strategy_max_yes_ask_dollars), cap)

    @property
    def trade_buy_max_yes_ask_implied_pct(self) -> float:
        """Effective max YES ask as implied probability 0–100 (uses ``trade_entry_effective_max_yes_ask_dollars``)."""
        return self.trade_entry_effective_max_yes_ask_dollars * 100.0

    @property
    def trade_entry_min_edge_from_50_pct_points(self) -> float:
        """Minimum |mid−50%| in percentage points (`TRADE_ENTRY_MIN_EDGE_FROM_50` × 100)."""
        return self.strategy_probability_gap * 100.0

    @property
    def trade_entry_min_yes_ask_cents_from_max_american_odds(self) -> int | None:
        """Minimum YES ask (¢) from positive American odds cap (+200 → ~34¢ implied). ``None`` if disabled.

        Maps +X to implied probability ``p = 100/(X+100)`` on a $1 contract, then minimum ask in cents is
        ``ceil(100*p)`` (same as ``ceil(10000/(X+100))``). This only gates **buys**; unrelated to stop-loss %.
        """
        a = float(self.trade_entry_max_american_odds_yes)
        if a <= 0:
            return None
        return max(1, min(98, int(math.ceil(100.0 * 100.0 / (a + 100.0) - 1e-12))))

    @property
    def trade_entry_effective_min_yes_ask_cents(self) -> int:
        """Strictest of ``TRADE_ENTRY_MIN_YES_ASK_CENTS`` (Kalshi **chance** ≈ this %% on $1) and American-odds cap."""
        floors: list[int] = []
        if self.trade_entry_min_yes_ask_cents > 0:
            floors.append(self.trade_entry_min_yes_ask_cents)
        am = self.trade_entry_min_yes_ask_cents_from_max_american_odds
        if am is not None:
            floors.append(am)
        return max(floors) if floors else 0

    @property
    def trade_entry_skip_substring_tokens(self) -> list[str]:
        """Uppercase tokens from ``TRADE_ENTRY_SKIP_TICKER_SUBSTRINGS`` (comma-separated)."""
        raw = (self.trade_entry_skip_ticker_substrings or "").strip()
        if not raw:
            return []
        return [x.strip().upper() for x in raw.split(",") if x.strip()]

    def auto_sell_effective_min_yes_bid_cents(self, cli_override: int | None) -> int | None:
        """Min best YES bid (cents) to treat as take-profit-by-implied-%, or None if only profit-margin mode."""
        if self.trade_exit_only_profit_margin:
            return None
        if cli_override is not None:
            return cli_override
        if self.auto_sell_min_yes_bid_cents is not None:
            return self.auto_sell_min_yes_bid_cents
        return int(round(self.trade_exit_take_profit_min_yes_bid_pct))

    @property
    def trade_exit_effective_min_profit_cents_per_contract(self) -> float | None:
        """Explicit min profit; if unset and profit-margin-only mode, fallback 1¢ ‘any green’ vs entry."""
        if self.trade_exit_min_profit_cents_per_contract is not None:
            return float(self.trade_exit_min_profit_cents_per_contract)
        if self.trade_exit_only_profit_margin:
            return 1.0
        return None

    def trade_exit_min_profit_cents_for_entry(self, entry_cents: int | None) -> float | None:
        """Min profit (¢/share) vs entry: max(floor, entry×pct), then optional cap. Used for take-profit."""
        fixed = self.trade_exit_effective_min_profit_cents_per_contract
        pct = float(self.trade_exit_min_profit_pct_of_entry)
        out: float | None = float(fixed) if fixed is not None else None
        if entry_cents is not None and entry_cents >= 1 and pct > 0:
            scaled = float(entry_cents) * pct
            if out is not None:
                out = max(out, scaled)
            else:
                out = scaled
        if out is None:
            return None
        cap = self.trade_exit_min_profit_cents_cap
        if cap is not None:
            out = min(out, float(cap))
        return max(0.0, out)


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
