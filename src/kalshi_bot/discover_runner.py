"""LLM watchlist filter + deterministic strategy rules (same as ``run``) across many tickers."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.config import Settings
from kalshi_bot.edge_math import implied_no_ask_dollars, implied_yes_ask_dollars
from kalshi_bot.execution import DryRunLedger
from kalshi_bot.logger import StructuredLogger, get_logger, maybe_clear_structured_log_after_tickers
from kalshi_bot.llm_screen import llm_discover_watchlist
from kalshi_bot.market_data import (
    fetch_yes_close_prices,
    get_orderbook,
    list_open_markets,
    summarize_market_row,
    yes_bid_and_no_bid_cents_for_trading,
)
from kalshi_bot.momentum import momentum_buy_intent_if_hot
from kalshi_bot.portfolio import PortfolioSnapshot, fetch_portfolio_snapshot, get_balance_cents
from kalshi_bot.risk import RiskManager
from kalshi_bot.strategy import (
    TradeIntent,
    choose_entry_side_and_ask_cents,
    entry_filter_timing_and_event,
    should_skip_buy_due_to_long_yes_cap,
    should_skip_buy_ticker_substrings,
    signal_edge_buy_no_from_ticker,
    signal_edge_buy_yes_from_ticker,
    signal_from_bar,
    signal_from_bar_buy_no,
    skip_buy_yes_longshot,
)
from kalshi_bot.trading import build_sdk_client, trade_execute
from kalshi_bot.bitcoin_runner import run_bitcoin_sidecar_if_due


@dataclass
class DiscoverRuleRunStats:
    markets: int = 0
    llm_excluded: int = 0
    llm_api_fail: int = 0
    skip_low_volume: int = 0
    skip_orderbook: int = 0
    skip_no_bids: int = 0
    skip_yes_ask_longshot: int = 0
    skip_ticker_substring: int = 0
    skip_long_yes_cap: int = 0
    skip_theta_decay: int = 0
    skip_event_not_top_yes: int = 0
    skip_multi_choice_not_top_n: int = 0
    skip_multi_choice_below_min: int = 0
    skip_multi_choice_not_in_event: int = 0
    no_rule_signal: int = 0
    momentum_signal: int = 0
    momentum_candle_error: int = 0
    skipped_cli_no_execute: int = 0
    blocked_trade_discover_auto_execute_false: int = 0
    submitted: int = 0
    bitcoin_sidecar_runs: int = 0
    bitcoin_sidecar_orders_submitted: int = 0

    def lines(self) -> list[str]:
        return [
            "--- discover-trade summary ---",
            f"  markets scanned:                    {self.markets}",
            f"  LLM watch=false (filtered out):     {self.llm_excluded}",
            f"  LLM API / JSON fail:                {self.llm_api_fail}",
            f"  skip (volume below min):            {self.skip_low_volume}",
            f"  skip (orderbook error):             {self.skip_orderbook}",
            f"  skip (no YES/NO bids):              {self.skip_no_bids}",
            f"  skip (YES ask < min / longshot):    {self.skip_yes_ask_longshot}",
            f"  skip (ticker substring block):      {self.skip_ticker_substring}",
            f"  skip (long-YES family cap):         {self.skip_long_yes_cap}",
            f"  skip (theta / near-exp longshot):   {self.skip_theta_decay}",
            f"  skip (not in event top-N YES):      {self.skip_event_not_top_yes}",
            f"  skip (multi-choice not top-N):      {self.skip_multi_choice_not_top_n}",
            f"  skip (multi-choice < min chance):   {self.skip_multi_choice_below_min}",
            f"  skip (multi-choice ticker missing):  {self.skip_multi_choice_not_in_event}",
            f"  momentum (chart YES) signals:       {self.momentum_signal}",
            f"  momentum candle fetch errors:       {self.momentum_candle_error}",
            f"  no signal from .env rules:          {self.no_rule_signal}",
            f"  skipped (--execute false):          {self.skipped_cli_no_execute}",
            f"  TRADE_DISCOVER_AUTO_EXECUTE false:  {self.blocked_trade_discover_auto_execute_false}",
            f"  reached trade_execute:              {self.submitted}",
            f"  bitcoin sidecar runs:               {self.bitcoin_sidecar_runs}",
            f"  bitcoin sidecar orders submitted:   {self.bitcoin_sidecar_orders_submitted}",
            "---",
        ]


def run_discover_rule_pipeline(
    settings: Settings,
    *,
    execute: bool,
    log: StructuredLogger | None = None,
    bitcoin_scan_counter: list[int] | None = None,
    bitcoin_rotation_counter: list[int] | None = None,
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

    counter = bitcoin_scan_counter if bitcoin_scan_counter is not None else [0]
    btc_rot = bitcoin_rotation_counter if bitcoin_rotation_counter is not None else [0]

    snap_for_cap: PortfolioSnapshot | None = None
    if settings.trade_entry_cap_long_yes_max > 0 and (settings.trade_entry_cap_long_yes_substring or "").strip():
        try:
            snap_for_cap = fetch_portfolio_snapshot(client, ticker=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("discover_portfolio_snapshot_fail", error=str(exc))
            snap_for_cap = None

    event_data_cache: dict[str, list[tuple[str, float]] | None] = {}

    for i, m in enumerate(markets):
        counter[0] += 1
        try:
            s = summarize_market_row(m)
            ticker = s.ticker
            title = s.title
            print(f"discover-trade: {ticker} …", flush=True)

            if settings.trade_min_market_volume is not None:
                vol = s.volume
                if vol is None or vol < settings.trade_min_market_volume:
                    stats.skip_low_volume += 1
                    log.info("discover_skip_low_volume", ticker=ticker, volume=vol)
                    continue

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

            yb_c, nb_c = yes_bid_and_no_bid_cents_for_trading(ob)
            if nb_c is None:
                stats.skip_no_bids += 1
                continue

            yes_bid_d = yb_c / 100.0
            yes_ask_d = implied_yes_ask_dollars(nb_c / 100.0)
            yes_ask_c = int(max(1, min(99, round(yes_ask_d * 100.0))))
            no_bid_d = nb_c / 100.0
            no_ask_d = implied_no_ask_dollars(yb_c / 100.0)
            entry_side, chosen_ask_c = choose_entry_side_and_ask_cents(
                settings, yes_ask_cents=yes_ask_c, yes_bid_cents=yb_c, no_bid_cents=nb_c
            )
            if should_skip_buy_ticker_substrings(settings, ticker):
                stats.skip_ticker_substring += 1
                log.info("discover_skip_ticker_substring", ticker=ticker)
                continue
            if snap_for_cap is not None and should_skip_buy_due_to_long_yes_cap(
                settings, ticker=ticker, snap=snap_for_cap
            ):
                stats.skip_long_yes_cap += 1
                log.info(
                    "discover_skip_long_yes_cap",
                    ticker=ticker,
                    cap=settings.trade_entry_cap_long_yes_max,
                    substring=(settings.trade_entry_cap_long_yes_substring or "").strip(),
                )
                continue
            if skip_buy_yes_longshot(settings, chosen_ask_c):
                stats.skip_yes_ask_longshot += 1
                log.info(
                    "discover_skip_longshot_yes",
                    ticker=ticker,
                    yes_ask_cents=yes_ask_c,
                    chosen_ask_cents=chosen_ask_c,
                    entry_side=entry_side,
                    min_yes_ask_cents=settings.trade_entry_effective_min_yes_ask_cents,
                )
                continue

            skip_te, te_reason = entry_filter_timing_and_event(
                settings, client, ticker, chosen_ask_c, event_data_cache
            )
            if skip_te:
                if te_reason == "theta_decay_longshot":
                    stats.skip_theta_decay += 1
                elif te_reason == "not_in_event_top_yes":
                    stats.skip_event_not_top_yes += 1
                elif te_reason == "multi_choice_not_top_n":
                    stats.skip_multi_choice_not_top_n += 1
                elif te_reason == "multi_choice_below_min_chance":
                    stats.skip_multi_choice_below_min += 1
                elif te_reason == "multi_choice_ticker_not_in_event":
                    stats.skip_multi_choice_not_in_event += 1
                log.info(
                    "discover_skip_entry_filter",
                    ticker=ticker,
                    reason=te_reason,
                    yes_ask_cents=yes_ask_c,
                    chosen_ask_cents=chosen_ask_c,
                    entry_side=entry_side,
                )
                continue

            intent: TradeIntent | None = None
            if entry_side == "yes" and settings.trade_momentum_enabled:
                try:
                    closes = fetch_yes_close_prices(
                        client,
                        ticker,
                        period_interval_minutes=settings.trade_momentum_period_minutes,
                        lookback_seconds=settings.trade_momentum_lookback_minutes * 60,
                    )
                except Exception as exc:  # noqa: BLE001
                    stats.momentum_candle_error += 1
                    log.warning("discover_momentum_candles_fail", ticker=ticker, error=str(exc))
                    closes = []
                if closes:
                    intent, _mwhy = momentum_buy_intent_if_hot(
                        ticker=ticker,
                        yes_bid_dollars=yes_bid_d,
                        yes_ask_dollars=yes_ask_d,
                        settings=settings,
                        close_prices=closes,
                    )
                    if intent is not None:
                        stats.momentum_signal += 1
                        log.info("discover_momentum_signal", ticker=ticker, note=_mwhy)

            if intent is None and settings.trade_use_edge_strategy and settings.trade_fair_yes_prob is not None:
                if entry_side == "yes":
                    intent = signal_edge_buy_yes_from_ticker(
                        ticker=ticker,
                        yes_bid_dollars=yes_bid_d,
                        yes_ask_dollars=yes_ask_d,
                        settings=settings,
                    )
                else:
                    intent = signal_edge_buy_no_from_ticker(
                        ticker=ticker,
                        no_bid_dollars=no_bid_d,
                        no_ask_dollars=no_ask_d,
                        settings=settings,
                    )
            elif intent is None:
                if entry_side == "yes":
                    intent = signal_from_bar(
                        ticker=ticker,
                        yes_bid_dollars=yes_bid_d,
                        yes_ask_dollars=yes_ask_d,
                        max_yes_ask_dollars=settings.strategy_max_yes_ask_dollars,
                        min_spread_dollars=settings.strategy_min_spread_dollars,
                        probability_gap=settings.strategy_probability_gap,
                        order_count=settings.strategy_order_count,
                        limit_price_cents=settings.strategy_limit_price_cents,
                        max_spread_dollars=settings.trade_max_entry_spread_dollars,
                        entry_min_yes_ask_cents=settings.trade_entry_effective_min_yes_ask_cents,
                    )
                else:
                    intent = signal_from_bar_buy_no(
                        ticker=ticker,
                        no_bid_dollars=no_bid_d,
                        no_ask_dollars=no_ask_d,
                        max_yes_ask_dollars=settings.strategy_max_yes_ask_dollars,
                        min_spread_dollars=settings.strategy_min_spread_dollars,
                        probability_gap=settings.strategy_probability_gap,
                        order_count=settings.strategy_order_count,
                        limit_price_cents=settings.strategy_limit_price_cents,
                        max_spread_dollars=settings.trade_max_entry_spread_dollars,
                        entry_min_yes_ask_cents=settings.trade_entry_effective_min_yes_ask_cents,
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
        finally:
            dr, bo = run_bitcoin_sidecar_if_due(
                settings,
                client=client,
                risk=risk,
                ledger=ledger,
                log=log,
                execute=execute,
                scan_counter=counter,
                rotation_counter=btc_rot,
                log_prefix="discover-trade",
            )
            stats.bitcoin_sidecar_runs += dr
            stats.bitcoin_sidecar_orders_submitted += bo
            maybe_clear_structured_log_after_tickers(
                log_path=settings.structured_log_path,
                every_n=settings.structured_log_clear_every_n_tickers,
                processed_count=i + 1,
                log=log,
            )

    return stats.submitted + stats.bitcoin_sidecar_orders_submitted, stats
