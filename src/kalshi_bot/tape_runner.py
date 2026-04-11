"""Public **aggregate** trade-tape ranking → same .env rules as ``run`` / ``discover-trade``.

Kalshi **does not** expose other users' identities, profit, or per-account history in the API.
This ranks **markets** by recent anonymous flow (proxy for active interest), not “copy a profitable trader.”
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.config import Settings
from kalshi_bot.edge_math import implied_yes_ask_dollars
from kalshi_bot.execution import DryRunLedger
from kalshi_bot.logger import StructuredLogger, get_logger
from kalshi_bot.market_data import (
    best_no_bid_cents,
    best_yes_bid_cents,
    fetch_public_trades,
    get_market,
    get_orderbook,
    rank_tickers_by_public_flow,
    summarize_market_row,
)
from kalshi_bot.risk import RiskManager
from kalshi_bot.strategy import signal_edge_buy_yes_from_ticker, signal_from_bar
from kalshi_bot.trading import build_sdk_client, trade_execute


@dataclass
class TapeRuleRunStats:
    trades_fetched: int = 0
    tickers_ranked: int = 0
    skipped_min_flow: int = 0
    skip_low_volume: int = 0
    skip_orderbook: int = 0
    skip_no_bids: int = 0
    no_rule_signal: int = 0
    skipped_cli_no_execute: int = 0
    blocked_trade_tape_auto_execute_false: int = 0
    submitted: int = 0

    def lines(self) -> list[str]:
        return [
            "--- tape-trade summary ---",
            "  (Public tape has no user IDs — flow rank only, not “copy profitable people”.)",
            f"  public trades fetched:              {self.trades_fetched}",
            f"  distinct tickers ranked:          {self.tickers_ranked}",
            f"  skip (below TRADE_TAPE_MIN_FLOW):   {self.skipped_min_flow}",
            f"  skip (volume below min):            {self.skip_low_volume}",
            f"  skip (orderbook error):             {self.skip_orderbook}",
            f"  skip (no YES/NO bids):              {self.skip_no_bids}",
            f"  no signal from .env rules:          {self.no_rule_signal}",
            f"  skipped (--execute false):          {self.skipped_cli_no_execute}",
            f"  TRADE_TAPE_AUTO_EXECUTE false:      {self.blocked_trade_tape_auto_execute_false}",
            f"  reached trade_execute:              {self.submitted}",
            "---",
        ]


def run_tape_rule_pipeline(
    settings: Settings,
    *,
    execute: bool,
    log: StructuredLogger | None = None,
) -> tuple[int, TapeRuleRunStats]:
    """Rank tickers by recent public trade $ flow, then apply deterministic rules from .env."""
    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    stats = TapeRuleRunStats()

    client = build_sdk_client(settings)
    risk = RiskManager(settings)
    ledger = DryRunLedger()

    raw = fetch_public_trades(client, max_trades=settings.trade_tape_max_trades_fetch)
    stats.trades_fetched = len(raw)
    ranked = rank_tickers_by_public_flow(raw)
    stats.tickers_ranked = len(ranked)

    log.info(
        "tape_trade_start",
        trades_fetched=stats.trades_fetched,
        distinct_tickers=stats.tickers_ranked,
        top_n=settings.trade_tape_top_markets,
        execute=execute,
        trade_tape_auto_execute=settings.trade_tape_auto_execute,
        dry_run=settings.dry_run,
        live_trading=settings.live_trading,
    )

    top = ranked[: settings.trade_tape_top_markets]
    min_flow = settings.trade_tape_min_flow_usd

    for ticker, flow_usd, n_trades in top:
        print(f"tape-trade: {ticker} (flow≈${flow_usd:.2f}, {n_trades} trades) …", flush=True)

        if min_flow > 0.0 and flow_usd < min_flow:
            stats.skipped_min_flow += 1
            continue

        if settings.trade_min_market_volume is not None:
            try:
                mrow = get_market(client, ticker=ticker)
                m = getattr(mrow, "market", None)
                s = summarize_market_row(m) if m is not None else None
                vol = getattr(s, "volume", None) if s is not None else None
            except Exception:  # noqa: BLE001
                vol = None
            if vol is None or vol < settings.trade_min_market_volume:
                stats.skip_low_volume += 1
                log.info("tape_skip_low_volume", ticker=ticker, volume=vol)
                continue

        try:
            ob = get_orderbook(client, ticker)
        except Exception as exc:  # noqa: BLE001
            stats.skip_orderbook += 1
            log.warning("tape_skip_orderbook", ticker=ticker, error=str(exc))
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
                max_spread_dollars=settings.trade_max_entry_spread_dollars,
            )

        if intent is None:
            stats.no_rule_signal += 1
            continue

        if not execute:
            stats.skipped_cli_no_execute += 1
            log.warning(
                "tape_trade_candidate",
                ticker=ticker,
                count=intent.count,
                yes_price_cents=intent.yes_price_cents,
                note="re-run with --execute and TRADE_TAPE_AUTO_EXECUTE=true",
            )
            continue

        if not settings.trade_tape_auto_execute:
            stats.blocked_trade_tape_auto_execute_false += 1
            log.warning("tape_trade_blocked", ticker=ticker, reason="TRADE_TAPE_AUTO_EXECUTE_false")
            continue

        trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
        stats.submitted += 1

    return stats.submitted, stats
