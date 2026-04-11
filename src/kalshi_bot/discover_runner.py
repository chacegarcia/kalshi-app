"""LLM watchlist filter + deterministic strategy rules (same as ``run``) across many tickers."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.config import Settings
from kalshi_bot.edge_math import implied_yes_ask_dollars
from kalshi_bot.execution import DryRunLedger
from kalshi_bot.logger import StructuredLogger, get_logger
from kalshi_bot.llm_screen import llm_discover_watchlist
from kalshi_bot.market_data import best_no_bid_cents, best_yes_bid_cents, get_orderbook, list_open_markets, summarize_market_row
from kalshi_bot.portfolio import get_balance_cents
from kalshi_bot.risk import RiskManager
from kalshi_bot.strategy import signal_edge_buy_yes_from_ticker, signal_from_bar
from kalshi_bot.trading import build_sdk_client, trade_execute


@dataclass
class DiscoverRuleRunStats:
    markets: int = 0
    llm_excluded: int = 0
    llm_api_fail: int = 0
    skip_orderbook: int = 0
    skip_no_bids: int = 0
    no_rule_signal: int = 0
    skipped_cli_no_execute: int = 0
    blocked_trade_discover_auto_execute_false: int = 0
    submitted: int = 0

    def lines(self) -> list[str]:
        return [
            "--- discover-trade summary ---",
            f"  markets scanned:                    {self.markets}",
            f"  LLM watch=false (filtered out):     {self.llm_excluded}",
            f"  LLM API / JSON fail:                {self.llm_api_fail}",
            f"  skip (orderbook error):             {self.skip_orderbook}",
            f"  skip (no YES/NO bids):              {self.skip_no_bids}",
            f"  no signal from .env rules:          {self.no_rule_signal}",
            f"  skipped (--execute false):          {self.skipped_cli_no_execute}",
            f"  TRADE_DISCOVER_AUTO_EXECUTE false:  {self.blocked_trade_discover_auto_execute_false}",
            f"  reached trade_execute:              {self.submitted}",
            "---",
        ]


def run_discover_rule_pipeline(
    settings: Settings,
    *,
    execute: bool,
    log: StructuredLogger | None = None,
) -> tuple[int, DiscoverRuleRunStats]:
    """LLM filters titles → REST order book → same rules as ``SampleSpreadGapStrategy`` (from .env)."""
    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    stats = DiscoverRuleRunStats()

    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for discover-trade")

    client = build_sdk_client(settings)
    bal = get_balance_cents(client)
    risk = RiskManager(settings)
    ledger = DryRunLedger()

    resp = list_open_markets(client, limit=settings.trade_llm_max_markets_per_run)
    markets = list(getattr(resp, "markets", []) or [])
    stats.markets = len(markets)
    log.info(
        "discover_trade_start",
        market_count=len(markets),
        execute=execute,
        trade_discover_auto_execute=settings.trade_discover_auto_execute,
        dry_run=settings.dry_run,
        live_trading=settings.live_trading,
    )

    for m in markets:
        s = summarize_market_row(m)
        ticker = s.ticker
        title = s.title
        print(f"discover-trade: {ticker} …", flush=True)

        disc = llm_discover_watchlist(settings, ticker=ticker, title=title)
        if disc is None:
            stats.llm_api_fail += 1
            log.warning("discover_llm_fail", ticker=ticker)
            continue
        log.info("discover_llm", ticker=ticker, watch=disc.watch, reason=disc.reason[:300])
        if not disc.watch:
            stats.llm_excluded += 1
            continue

        try:
            ob = get_orderbook(client, ticker)
        except Exception as exc:  # noqa: BLE001
            stats.skip_orderbook += 1
            log.warning("discover_skip_orderbook", ticker=ticker, error=str(exc))
            continue

        yb_c = best_yes_bid_cents(ob)
        nb_c = best_no_bid_cents(ob)
        if yb_c is None or nb_c is None:
            stats.skip_no_bids += 1
            continue

        yes_bid_d = yb_c / 100.0
        yes_ask_d = implied_yes_ask_dollars(nb_c / 100.0)

        if settings.trade_use_edge_strategy and settings.trade_fair_yes_prob is not None:
            intent = signal_edge_buy_yes_from_ticker(
                ticker=ticker,
                yes_bid_dollars=yes_bid_d,
                yes_ask_dollars=yes_ask_d,
                settings=settings,
            )
        else:
            intent = signal_from_bar(
                ticker=ticker,
                yes_bid_dollars=yes_bid_d,
                yes_ask_dollars=yes_ask_d,
                max_yes_ask_dollars=settings.strategy_max_yes_ask_dollars,
                min_spread_dollars=settings.strategy_min_spread_dollars,
                probability_gap=settings.strategy_probability_gap,
                order_count=settings.strategy_order_count,
                limit_price_cents=settings.strategy_limit_price_cents,
            )

        if intent is None:
            stats.no_rule_signal += 1
            continue

        if not execute:
            stats.skipped_cli_no_execute += 1
            log.warning(
                "discover_trade_candidate",
                ticker=ticker,
                count=intent.count,
                yes_price_cents=intent.yes_price_cents,
                note="re-run with --execute and TRADE_DISCOVER_AUTO_EXECUTE=true",
            )
            continue

        if not settings.trade_discover_auto_execute:
            stats.blocked_trade_discover_auto_execute_false += 1
            log.warning("discover_trade_blocked", ticker=ticker, reason="TRADE_DISCOVER_AUTO_EXECUTE_false")
            continue

        trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
        stats.submitted += 1

    return stats.submitted, stats
