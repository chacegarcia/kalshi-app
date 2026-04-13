"""CLI: live trading loop, WebSocket watch, backtest, sweep, walk-forward."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from kalshi_bot.client import KalshiSdkClient

from kalshi_bot.ssl_bundle import apply_certifi_ca_bundle
from kalshi_bot.auth import AuthError, build_kalshi_auth
from kalshi_bot.auto_sell import (
    auto_sell_scan_all_long_yes,
    collect_exit_scan_rows,
    format_exit_scan_summary,
    liquidate_all_long_yes_positions,
    run_auto_sell_loop,
)
from kalshi_bot.backtest import load_price_records_jsonl, parameter_sweep, run_rule_backtest, walk_forward_eval
from kalshi_bot.config import Settings, get_settings, project_root
from kalshi_bot.execution import (
    DryRunLedger,
    cancel_all_resting_orders,
    cancel_stale_orders,
    execute_intent,
)
from kalshi_bot.logger import StructuredLogger, get_logger, maybe_clear_structured_log_every_other_pass
from kalshi_bot.market_data import list_open_markets, summarize_market_row
from kalshi_bot.metrics import NO_GUARANTEE_DISCLAIMER, fee_slippage_sensitivity
from kalshi_bot.scanner import format_scan_report, scan_kalshi_opportunities
from kalshi_bot.paper_engine import PaperFillConfig
from kalshi_bot.portfolio import fetch_portfolio_snapshot, print_portfolio_balance_line
from kalshi_bot.position_watch import (
    collect_position_watch_rows,
    format_position_watch_lines,
    rows_to_json,
    run_position_watch_loop,
)
from kalshi_bot.risk import RiskManager
from kalshi_bot.strategy import SampleSpreadGapStrategy, make_bar_strategy_fn
from kalshi_bot.discover_runner import run_discover_rule_pipeline
from kalshi_bot.tape_runner import run_tape_rule_pipeline
from kalshi_bot.bitcoin_runner import run_bitcoin_trade_pass
from kalshi_bot.monitor import (
    heartbeat,
    notify_pass_summary_to_dashboard,
    notify_portfolio_series_to_dashboard,
    record_portfolio_series_point,
    record_trade_pass_summary,
    start_dashboard,
    start_portfolio_series_poller,
)
from kalshi_bot.trading import build_sdk_client, make_limit_intent, trade_execute
from kalshi_bot.ws import KalshiWS


def _client_for_balance(settings: Settings, dash_client: KalshiSdkClient | None) -> KalshiSdkClient:
    """Reuse dashboard client or build one so balance prints even without --web."""
    return dash_client if dash_client is not None else build_sdk_client(settings)


def _record_trade_pass_for_dashboard(
    settings: Settings,
    *,
    command: str,
    iteration: int,
    orders_submitted: int,
    run_stats: Any,
    dash_client: KalshiSdkClient | None,
) -> None:
    """Update pass summary in-process (``--web``) or POST to a separate dashboard process."""
    try:
        stats = asdict(run_stats) if is_dataclass(run_stats) else {"repr": repr(run_stats)}
    except Exception:
        stats = {"error": "serialize_failed"}
    if dash_client is not None:
        record_trade_pass_summary(
            command=command,
            iteration=iteration,
            orders_submitted=orders_submitted,
            stats=stats,
        )
    else:
        notify_pass_summary_to_dashboard(
            settings,
            command=command,
            iteration=iteration,
            orders_submitted=orders_submitted,
            stats=stats,
        )


def _maybe_position_watch_before_auto_sell(settings: Settings, client: KalshiSdkClient, log: StructuredLogger) -> None:
    """Same table as ``positions-watch`` (book + tape lean), printed before exit-scan — no extra terminal."""
    if not settings.trade_position_watch_before_auto_sell:
        return
    try:
        rows = collect_position_watch_rows(
            client,
            settings,
            max_trades_per_ticker=max(50, int(settings.trade_exit_tape_lookback_max_trades)),
            include_candles=False,
            log=log,
        )
        if not rows:
            return
        print("--- position watch (before auto-sell) ---", flush=True)
        for line in format_position_watch_lines(rows):
            print(line, flush=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("position_watch_before_auto_sell_fail", error=str(exc))
        print(f"position watch skipped: {exc}", flush=True)


def _maybe_exit_scan_after_pass(settings: Settings, client: KalshiSdkClient, log: StructuredLogger) -> None:
    """After a trading pass summary, optionally scan all long YES and submit take-profit sells (TRADE_EXIT_*)."""
    if not settings.trade_auto_sell_after_each_pass:
        return
    _maybe_position_watch_before_auto_sell(settings, client, log)
    try:
        n, lines = auto_sell_scan_all_long_yes(
            client, settings, cli_min_yes_bid_cents=None, log=log
        )
    except ValueError as exc:
        print(f"exit-scan skipped: {exc}", flush=True)
        return
    print("--- exit scan (take-profit) ---", flush=True)
    if n == 0 and not lines:
        print(
            "exit-scan: no sells (no long YES, or bid/entry rules not met — tune TRADE_EXIT_* / TRADE_TAKE_PROFIT_*).",
            flush=True,
        )
    for line in lines:
        print(line, flush=True)


def cmd_llm_trade(
    settings: Settings,
    *,
    execute: bool,
    loop: bool = False,
    interval_seconds: float = 120.0,
    use_tape: bool = False,
) -> None:
    print(NO_GUARANTEE_DISCLAIMER)
    print()
    from kalshi_bot.llm_runner import LLMTradeRunStats, run_llm_opportunity_pipeline

    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)

    dash_client = None
    if settings.dashboard_enabled:
        start_dashboard(settings)
        dash_client = build_sdk_client(settings)
        start_portfolio_series_poller(settings, dash_client)
        try:
            snap0 = fetch_portfolio_snapshot(dash_client, ticker=None)
            record_portfolio_series_point(
                snap0.balance_cents,
                snap0.portfolio_value_cents,
                exposure_sum_cents=float(snap0.total_exposure_cents),
            )
        except Exception:
            pass

    iteration = 0
    try:
        while True:
            iteration += 1
            if loop:
                print(f"--- llm-trade iteration {iteration} ---", flush=True)
            n = 0
            run_stats: LLMTradeRunStats | None = None
            try:
                n, run_stats = run_llm_opportunity_pipeline(
                    settings,
                    execute=execute,
                    log=log,
                    use_tape_universe=use_tape,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(2)
            except Exception as exc:
                print(f"\nllm-trade pipeline crashed: {exc}", file=sys.stderr)
                traceback.print_exc()
                err = LLMTradeRunStats()
                err.cli_execute = execute
                err.dry_run = settings.dry_run
                err.live_trading = settings.live_trading
                err.trade_llm_auto_execute = settings.trade_llm_auto_execute
                err.pipeline_error = f"{type(exc).__name__}: {exc}"
                n = 0
                run_stats = err
            assert run_stats is not None
            print(f"Orders submitted this run (0 = none or scan-only): {n}")
            summary_lines = run_stats.lines()
            for line in summary_lines:
                print(line, flush=True)
            _record_trade_pass_for_dashboard(
                settings,
                command="llm-trade",
                iteration=iteration,
                orders_submitted=n,
                run_stats=run_stats,
                dash_client=dash_client,
            )
            bal_client = _client_for_balance(settings, dash_client)
            print_portfolio_balance_line(bal_client)
            _maybe_exit_scan_after_pass(settings, bal_client, log)
            if dash_client is not None:
                try:
                    snap = fetch_portfolio_snapshot(dash_client, ticker=None)
                    record_portfolio_series_point(
                        snap.balance_cents,
                        snap.portfolio_value_cents,
                        exposure_sum_cents=float(snap.total_exposure_cents),
                    )
                except Exception:
                    pass
            else:
                notify_portfolio_series_to_dashboard(settings)
            maybe_clear_structured_log_every_other_pass(
                log_path=settings.structured_log_path,
                pass_number=iteration,
                enabled=settings.structured_log_clear_every_other_pass,
                log=log,
            )
            if not loop:
                break
            print(f"Sleeping {interval_seconds:.0f}s… (Ctrl+C to stop)\n", flush=True)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\nllm-trade loop stopped.", file=sys.stderr)


def cmd_discover_trade(
    settings: Settings,
    *,
    execute: bool,
    loop: bool = False,
    interval_seconds: float = 120.0,
) -> None:
    print(NO_GUARANTEE_DISCLAIMER)
    print()
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)

    dash_client = None
    if settings.dashboard_enabled:
        start_dashboard(settings)
        dash_client = build_sdk_client(settings)
        start_portfolio_series_poller(settings, dash_client)
        try:
            snap0 = fetch_portfolio_snapshot(dash_client, ticker=None)
            record_portfolio_series_point(
                snap0.balance_cents,
                snap0.portfolio_value_cents,
                exposure_sum_cents=float(snap0.total_exposure_cents),
            )
        except Exception:
            pass

    bitcoin_scan_counter: list[int] = [0]
    bitcoin_rotation_counter: list[int] = [0]
    iteration = 0
    try:
        while True:
            iteration += 1
            if loop:
                print(f"--- discover-trade iteration {iteration} ---", flush=True)
            try:
                n, run_stats = run_discover_rule_pipeline(
                    settings,
                    execute=execute,
                    log=log,
                    bitcoin_scan_counter=bitcoin_scan_counter,
                    bitcoin_rotation_counter=bitcoin_rotation_counter,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(2)
            print(f"Orders submitted this run (0 = none): {n}")
            summary_lines = run_stats.lines()
            for line in summary_lines:
                print(line, flush=True)
            _record_trade_pass_for_dashboard(
                settings,
                command="discover-trade",
                iteration=iteration,
                orders_submitted=n,
                run_stats=run_stats,
                dash_client=dash_client,
            )
            bal_client = _client_for_balance(settings, dash_client)
            print_portfolio_balance_line(bal_client)
            _maybe_exit_scan_after_pass(settings, bal_client, log)
            if dash_client is not None:
                try:
                    snap = fetch_portfolio_snapshot(dash_client, ticker=None)
                    record_portfolio_series_point(
                        snap.balance_cents,
                        snap.portfolio_value_cents,
                        exposure_sum_cents=float(snap.total_exposure_cents),
                    )
                except Exception:
                    pass
            else:
                notify_portfolio_series_to_dashboard(settings)
            maybe_clear_structured_log_every_other_pass(
                log_path=settings.structured_log_path,
                pass_number=iteration,
                enabled=settings.structured_log_clear_every_other_pass,
                log=log,
            )
            if not loop:
                break
            print(f"Sleeping {interval_seconds:.0f}s… (Ctrl+C to stop)\n", flush=True)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\ndiscover-trade loop stopped.", file=sys.stderr)


def cmd_tape_trade(
    settings: Settings,
    *,
    execute: bool,
    loop: bool = False,
    interval_seconds: float = 120.0,
) -> None:
    print(NO_GUARANTEE_DISCLAIMER)
    print(
        "Note: Kalshi public trades have no user IDs. tape-trade ranks markets by anonymous flow, "
        "then applies your .env rules — it does not copy specific profitable accounts.\n",
        flush=True,
    )
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)

    dash_client = None
    if settings.dashboard_enabled:
        start_dashboard(settings)
        dash_client = build_sdk_client(settings)
        start_portfolio_series_poller(settings, dash_client)
        try:
            snap0 = fetch_portfolio_snapshot(dash_client, ticker=None)
            record_portfolio_series_point(
                snap0.balance_cents,
                snap0.portfolio_value_cents,
                exposure_sum_cents=float(snap0.total_exposure_cents),
            )
        except Exception:
            pass

    bitcoin_scan_counter: list[int] = [0]
    bitcoin_rotation_counter: list[int] = [0]
    iteration = 0
    try:
        while True:
            iteration += 1
            if loop:
                print(f"--- tape-trade iteration {iteration} ---", flush=True)
            try:
                n, run_stats = run_tape_rule_pipeline(
                    settings,
                    execute=execute,
                    log=log,
                    bitcoin_scan_counter=bitcoin_scan_counter,
                    bitcoin_rotation_counter=bitcoin_rotation_counter,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(2)
            print(f"Orders submitted this run (0 = none): {n}")
            summary_lines = run_stats.lines()
            for line in summary_lines:
                print(line, flush=True)
            _record_trade_pass_for_dashboard(
                settings,
                command="tape-trade",
                iteration=iteration,
                orders_submitted=n,
                run_stats=run_stats,
                dash_client=dash_client,
            )
            bal_client = _client_for_balance(settings, dash_client)
            print_portfolio_balance_line(bal_client)
            _maybe_exit_scan_after_pass(settings, bal_client, log)
            if dash_client is not None:
                try:
                    snap = fetch_portfolio_snapshot(dash_client, ticker=None)
                    record_portfolio_series_point(
                        snap.balance_cents,
                        snap.portfolio_value_cents,
                        exposure_sum_cents=float(snap.total_exposure_cents),
                    )
                except Exception:
                    pass
            else:
                notify_portfolio_series_to_dashboard(settings)
            maybe_clear_structured_log_every_other_pass(
                log_path=settings.structured_log_path,
                pass_number=iteration,
                enabled=settings.structured_log_clear_every_other_pass,
                log=log,
            )
            if not loop:
                break
            print(f"Sleeping {interval_seconds:.0f}s… (Ctrl+C to stop)\n", flush=True)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\ntape-trade loop stopped.", file=sys.stderr)


def cmd_bitcoin_trade(
    settings: Settings,
    *,
    execute: bool,
    loop: bool = False,
    interval_seconds: float = 45.0,
) -> None:
    print(NO_GUARANTEE_DISCLAIMER)
    print(
        "bitcoin-trade: public BTC/ETH spot (TRADE_CRYPTO_SPOT_PRICE_SOURCE: auto/coingecko/binance) + Kalshi crypto "
        "binaries. Pin TRADE_BITCOIN_KALSHI_TICKER for one market, or leave empty and set "
        "TRADE_CRYPTO_KALSHI_PREFIXES (e.g. KXBTC,KXETH) or TRADE_BITCOIN_TICKER_PREFIX — contracts roll frequently.\n",
        flush=True,
    )
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)

    dash_client = None
    if settings.dashboard_enabled:
        start_dashboard(settings)
        dash_client = build_sdk_client(settings)
        start_portfolio_series_poller(settings, dash_client)
        try:
            snap0 = fetch_portfolio_snapshot(dash_client, ticker=None)
            record_portfolio_series_point(
                snap0.balance_cents,
                snap0.portfolio_value_cents,
                exposure_sum_cents=float(snap0.total_exposure_cents),
            )
        except Exception:
            pass

    bitcoin_rotation_counter: list[int] = [0]
    iteration = 0
    try:
        while True:
            iteration += 1
            if loop:
                print(f"--- bitcoin-trade iteration {iteration} ---", flush=True)
            try:
                n, run_stats = run_bitcoin_trade_pass(
                    settings,
                    execute=execute,
                    log=log,
                    rotation_counter=bitcoin_rotation_counter,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(2)
            print(f"Orders submitted this run (0 = none): {n}")
            summary_lines = run_stats.lines()
            for line in summary_lines:
                print(line, flush=True)
            _record_trade_pass_for_dashboard(
                settings,
                command="bitcoin-trade",
                iteration=iteration,
                orders_submitted=n,
                run_stats=run_stats,
                dash_client=dash_client,
            )
            bal_client = _client_for_balance(settings, dash_client)
            print_portfolio_balance_line(bal_client)
            _maybe_exit_scan_after_pass(settings, bal_client, log)
            if dash_client is not None:
                try:
                    snap = fetch_portfolio_snapshot(dash_client, ticker=None)
                    record_portfolio_series_point(
                        snap.balance_cents,
                        snap.portfolio_value_cents,
                        exposure_sum_cents=float(snap.total_exposure_cents),
                    )
                except Exception:
                    pass
            else:
                notify_portfolio_series_to_dashboard(settings)
            maybe_clear_structured_log_every_other_pass(
                log_path=settings.structured_log_path,
                pass_number=iteration,
                enabled=settings.structured_log_clear_every_other_pass,
                log=log,
            )
            if not loop:
                break
            print(f"Sleeping {interval_seconds:.0f}s… (Ctrl+C to stop)\n", flush=True)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\nbitcoin-trade loop stopped.", file=sys.stderr)


def cmd_scan(settings: Settings, *, limit: int, use_llm: bool) -> None:
    print(NO_GUARANTEE_DISCLAIMER)
    print()
    client = build_sdk_client(settings)
    rows = scan_kalshi_opportunities(client, settings, limit=limit, use_llm_fair=use_llm)
    print(format_scan_report(rows))
    print()
    print(
        "boxed$_after_fees: 1.0 − YES_ask − NO_ask − taker fees (Kalshi formula). "
        "edge_vs_fair: TRADE_FAIR_YES_PROB − YES_ask − fee (needs TRADE_FAIR_YES_PROB or --llm)."
    )


def cmd_list_markets(settings: Settings) -> None:
    client = build_sdk_client(settings)
    resp = list_open_markets(client, limit=30)
    markets = getattr(resp, "markets", []) or []
    for m in markets:
        s = summarize_market_row(m)
        print(f"{s.ticker}\t{s.status}\t{s.title[:80]}")


def cmd_watch_market(settings: Settings, ticker: str) -> None:
    auth = build_kalshi_auth(
        settings.kalshi_api_key_id,
        key_path=settings.kalshi_private_key_path,
        key_pem=settings.kalshi_private_key_pem,
    )

    async def on_message(msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t in ("ticker", "orderbook_snapshot", "orderbook_delta", "subscribed", "error"):
            print(msg)

    ws = KalshiWS(ws_url=settings.ws_url, auth=auth, on_message=on_message)
    asyncio.run(ws.run(market_tickers=[ticker]))


def cmd_place_test_order(settings: Settings, ticker: str | None) -> None:
    t = ticker or settings.strategy_market_ticker
    if not t:
        print("Set TRADE_MARKET_TICKER (or STRATEGY_MARKET_TICKER) or pass --ticker", file=sys.stderr)
        sys.exit(2)
    client = build_sdk_client(settings)
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    intent = make_limit_intent(
        ticker=t,
        side="yes",
        action="buy",
        count=1,
        yes_price_cents=settings.strategy_limit_price_cents,
    )
    trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)


def cmd_trade(
    settings: Settings,
    *,
    ticker: str,
    side: str,
    action: str,
    count: int,
    yes_price_cents: int | None,
) -> None:
    client = build_sdk_client(settings)
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    price = yes_price_cents if yes_price_cents is not None else settings.strategy_limit_price_cents
    intent = make_limit_intent(
        ticker=ticker,
        side=side,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        count=count,
        yes_price_cents=price,
    )
    trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)


def cmd_exit_scan(
    settings: Settings,
    *,
    min_yes_bid_cents: int | None,
    execute: bool,
    loop: bool = False,
    interval_seconds: float = 30.0,
) -> None:
    """Print a read-only cashout / take-profit report for all long YES; optional --execute to run batch auto-sell."""
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    iteration = 0
    try:
        while True:
            iteration += 1
            if loop:
                print(f"--- exit-scan iteration {iteration} ---", flush=True)
            client = build_sdk_client(settings)
            _maybe_position_watch_before_auto_sell(settings, client, log)
            rows = collect_exit_scan_rows(client, settings, cli_min_yes_bid_cents=min_yes_bid_cents, log=log)
            for line in format_exit_scan_summary(rows):
                print(line, flush=True)
            if not execute:
                if not loop:
                    print(
                        "  Run with --execute to submit take-profit sells where rules pass (same as post-pass exit scan).",
                        flush=True,
                    )
                elif iteration == 1:
                    print(
                        "  Loop: add --execute to submit take-profit sells after each summary when rules pass.",
                        flush=True,
                    )
            else:
                n, sell_lines = auto_sell_scan_all_long_yes(
                    client, settings, cli_min_yes_bid_cents=min_yes_bid_cents, log=log
                )
                print("--- exit-scan --execute ---", flush=True)
                if n == 0:
                    print(
                        "  No take-profit orders submitted (conditions not met, dry-run, or risk blocked).",
                        flush=True,
                    )
                else:
                    for sl in sell_lines:
                        print(f"  {sl}", flush=True)
                print(f"  Submitted sells: {n}", flush=True)

            if not loop:
                break
            print(f"Sleeping {interval_seconds:.0f}s… (Ctrl+C to stop)\n", flush=True)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\nexit-scan loop stopped.", file=sys.stderr)


def cmd_sell_bot(
    settings: Settings,
    *,
    min_yes_bid_cents: int | None,
    interval_seconds: float,
    execute: bool,
    web: bool = False,
) -> None:
    """Dedicated process: same as ``exit-scan --loop``. Does **not** open the Flask monitor unless ``--web``."""
    print(NO_GUARANTEE_DISCLAIMER)
    print()
    dash_client = None
    if web:
        start_dashboard(settings)
        dash_client = build_sdk_client(settings)
        start_portfolio_series_poller(settings, dash_client)
        try:
            snap0 = fetch_portfolio_snapshot(dash_client, ticker=None)
            record_portfolio_series_point(
                snap0.balance_cents,
                snap0.portfolio_value_cents,
                exposure_sum_cents=float(snap0.total_exposure_cents),
            )
        except Exception:
            pass
    print(
        "sell-bot: exit loop — same rules as post-pass auto-sell (TRADE_EXIT_*). "
        + ("Submitting sells when rules pass." if execute else "Read-only summary (no sells).")
        + f" Every {interval_seconds:.0f}s.\n"
        "Tip: set TRADE_AUTO_SELL_AFTER_EACH_PASS=false on your trade command so that process only scans/opens positions; "
        "this command owns exits.\n",
        flush=True,
    )
    cmd_exit_scan(
        settings,
        min_yes_bid_cents=min_yes_bid_cents,
        execute=execute,
        loop=True,
        interval_seconds=interval_seconds,
    )


def cmd_sell_all(settings: Settings, *, execute: bool) -> None:
    """Flatten all long YES: IOC-style limits at best bid (ignores take-profit / stop rules)."""
    print(NO_GUARANTEE_DISCLAIMER)
    print()
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    client = build_sdk_client(settings)
    n, lines = liquidate_all_long_yes_positions(client, settings, log=log, execute=execute)
    print("--- sell-all (liquidate long YES) ---", flush=True)
    for line in lines:
        print(line, flush=True)
    if execute:
        print(f"Orders submitted (accepted by bot path): {n}", flush=True)
        if settings.dry_run:
            print("  (DRY_RUN=true — simulated; set DRY_RUN=false + LIVE_TRADING=true for real sells.)", flush=True)
    else:
        print(
            f"Planned exits: {len(lines)}  (re-run with --execute to run through trade_execute; live needs LIVE_TRADING + not DRY_RUN)",
            flush=True,
        )


def cmd_auto_sell(
    settings: Settings,
    *,
    ticker: str | None,
    min_yes_bid_cents: int | None,
    poll_seconds: float | None,
    max_cycles: int,
    once: bool,
) -> None:
    t = ticker or settings.strategy_market_ticker
    if not t:
        print("Pass a ticker argument or set TRADE_MARKET_TICKER in .env", file=sys.stderr)
        sys.exit(2)
    poll = poll_seconds if poll_seconds is not None else settings.auto_sell_poll_seconds
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    eff = settings.auto_sell_effective_min_yes_bid_cents(min_yes_bid_cents)
    log.warning(
        "auto_sell_start",
        ticker=t,
        cli_min_yes_bid_cents=min_yes_bid_cents,
        effective_min_yes_bid_cents=eff,
        take_profit_min_yes_bid_pct=settings.trade_exit_take_profit_min_yes_bid_pct,
        min_profit_cents=settings.trade_exit_effective_min_profit_cents_per_contract,
        sell_time_in_force=settings.trade_exit_sell_time_in_force,
        poll_seconds=poll,
        max_cycles=max_cycles,
        once=once,
    )
    try:
        run_auto_sell_loop(
            settings,
            ticker=t,
            cli_min_yes_bid_cents=min_yes_bid_cents,
            poll_seconds=poll,
            max_cycles=max_cycles,
            stop_after_one_sell=once,
            log=log,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)


def cmd_cancel_all(settings: Settings) -> None:
    client = build_sdk_client(settings)
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    n = cancel_all_resting_orders(client, log)
    print(f"Requested cancel for {n} orders.")


def cmd_run_bot(settings: Settings) -> None:
    if not settings.strategy_market_ticker:
        print("TRADE_MARKET_TICKER is required for run", file=sys.stderr)
        sys.exit(2)

    start_dashboard(settings)

    auth = build_kalshi_auth(
        settings.kalshi_api_key_id,
        key_path=settings.kalshi_private_key_path,
        key_pem=settings.kalshi_private_key_pem,
    )
    client = build_sdk_client(settings)
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    strategy = SampleSpreadGapStrategy(settings)
    last_signal = 0.0

    if settings.dashboard_enabled:
        try:
            snap0 = fetch_portfolio_snapshot(client, ticker=settings.strategy_market_ticker)
            record_portfolio_series_point(
                snap0.balance_cents,
                snap0.portfolio_value_cents,
                exposure_sum_cents=float(snap0.total_exposure_cents),
            )
        except Exception:
            pass

    async def maintenance_loop() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                snap = fetch_portfolio_snapshot(client, ticker=settings.strategy_market_ticker)
                risk.record_balance_sample(snap.balance_cents)
                cancel_stale_orders(client, settings, log)
                if settings.dashboard_enabled:
                    record_portfolio_series_point(
                        snap.balance_cents,
                        snap.portfolio_value_cents,
                        exposure_sum_cents=float(snap.total_exposure_cents),
                    )
                    heartbeat(
                        f"balance_cents={snap.balance_cents} portfolio_value_cents={snap.portfolio_value_cents} "
                        f"exposure_sum_cents={snap.total_exposure_cents:.0f}"
                    )
            except Exception as exc:  # noqa: BLE001
                log.error("maintenance_loop_error", error=str(exc))

    async def on_message(msg: dict[str, Any]) -> None:
        nonlocal last_signal
        intent = strategy.on_ticker_message(msg)
        if intent is None:
            return
        now = time.time()
        if now - last_signal < settings.strategy_min_seconds_between_signals:
            return
        last_signal = now
        execute_intent(
            client=client,
            settings=settings,
            risk=risk,
            log=log,
            intent=intent,
            ledger=ledger,
        )

    async def _run() -> None:
        maint = asyncio.create_task(maintenance_loop())
        ws = KalshiWS(ws_url=settings.ws_url, auth=auth, on_message=on_message)
        try:
            await ws.run(market_tickers=[settings.strategy_market_ticker])
        finally:
            maint.cancel()

    log.info(
        "bot_start",
        dry_run=settings.dry_run,
        live_trading=settings.live_trading,
        env=settings.kalshi_env,
        market=settings.strategy_market_ticker,
    )
    if settings.dashboard_enabled:
        heartbeat("bot started")
    asyncio.run(_run())


def cmd_backtest(settings: Settings, path: Path) -> None:
    records = load_price_records_jsonl(path)
    cfg = PaperFillConfig(
        fee_cents_per_contract=settings.paper_fee_cents_per_contract,
        slippage_cents_per_contract=settings.paper_slippage_cents_per_contract,
        fill_probability_if_crossed=settings.paper_fill_probability,
    )
    params = {
        "ticker": settings.strategy_market_ticker or "BACKTEST",
        "max_yes_ask_dollars": settings.trade_entry_effective_max_yes_ask_dollars,
        "min_spread_dollars": settings.strategy_min_spread_dollars,
        "probability_gap": settings.strategy_probability_gap,
        "order_count": settings.strategy_order_count,
        "limit_price_cents": settings.strategy_limit_price_cents,
        "max_spread_dollars": settings.trade_max_entry_spread_dollars,
    }
    tr, eq, _ = run_rule_backtest(records, strategy_signal_fn=make_bar_strategy_fn(params), paper_cfg=cfg)
    from kalshi_bot.metrics import format_report

    print(format_report(trades=tr, equity_cents=eq))


def cmd_sweep(settings: Settings, path: Path) -> None:
    records = load_price_records_jsonl(path)
    cfg = PaperFillConfig(
        fee_cents_per_contract=settings.paper_fee_cents_per_contract,
        slippage_cents_per_contract=settings.paper_slippage_cents_per_contract,
        fill_probability_if_crossed=settings.paper_fill_probability,
    )
    grid = {
        "max_yes_ask_dollars": [0.45, 0.55, 0.65],
        "min_spread_dollars": [0.0, 0.02],
        "probability_gap": [0.0, 0.05],
        "order_count": [settings.strategy_order_count],
        "limit_price_cents": [settings.strategy_limit_price_cents],
        "ticker": [settings.strategy_market_ticker or "BACKTEST"],
    }

    def factory(p: dict[str, Any]):
        return make_bar_strategy_fn(p)

    rows = parameter_sweep(records, grid=grid, strategy_factory=factory, paper_cfg=cfg)
    for r in rows[:20]:
        print("---")
        print(r["params"])
        print(r["report"])


def cmd_positions_watch(
    settings: Settings,
    *,
    loop: bool,
    interval_seconds: float,
    json_mode: bool,
    include_candles: bool,
    max_trades_per_ticker: int,
) -> None:
    """Poll each long-YES: orderbook mid/spread, per-ticker public tape lean, optional candle drift."""
    print(NO_GUARANTEE_DISCLAIMER)
    print()
    client = build_sdk_client(settings)
    if loop:
        try:
            run_position_watch_loop(
                client,
                settings,
                interval_seconds=max(5.0, float(interval_seconds)),
                json_mode=json_mode,
                include_candles=include_candles,
                max_trades_per_ticker=max(10, int(max_trades_per_ticker)),
            )
        except KeyboardInterrupt:
            print("\npositions-watch stopped.", file=sys.stderr)
        return
    rows = collect_position_watch_rows(
        client,
        settings,
        max_trades_per_ticker=max(10, int(max_trades_per_ticker)),
        include_candles=include_candles,
    )
    if json_mode:
        print(rows_to_json(rows), flush=True)
    else:
        for line in format_position_watch_lines(rows):
            print(line, flush=True)


def cmd_walk_forward(settings: Settings, path: Path) -> None:
    print(NO_GUARANTEE_DISCLAIMER)
    records = load_price_records_jsonl(path)
    cfg = PaperFillConfig(
        fee_cents_per_contract=settings.paper_fee_cents_per_contract,
        slippage_cents_per_contract=settings.paper_slippage_cents_per_contract,
        fill_probability_if_crossed=settings.paper_fill_probability,
    )
    params = {
        "ticker": settings.strategy_market_ticker or "BACKTEST",
        "max_yes_ask_dollars": settings.trade_entry_effective_max_yes_ask_dollars,
        "min_spread_dollars": settings.strategy_min_spread_dollars,
        "probability_gap": settings.strategy_probability_gap,
        "order_count": settings.strategy_order_count,
        "limit_price_cents": settings.strategy_limit_price_cents,
        "max_spread_dollars": settings.trade_max_entry_spread_dollars,
    }
    out = walk_forward_eval(
        records,
        n_windows=4,
        train_ratio=0.7,
        strategy_factory=lambda _: make_bar_strategy_fn(params),
        param=params,
        paper_cfg=cfg,
    )
    for row in out:
        print(row)


def cmd_sensitivity(settings: Settings, path: Path) -> None:
    records = load_price_records_jsonl(path)
    cfg = PaperFillConfig(
        fee_cents_per_contract=settings.paper_fee_cents_per_contract,
        slippage_cents_per_contract=settings.paper_slippage_cents_per_contract,
        fill_probability_if_crossed=settings.paper_fill_probability,
    )
    params = {
        "ticker": settings.strategy_market_ticker or "BACKTEST",
        "max_yes_ask_dollars": settings.trade_entry_effective_max_yes_ask_dollars,
        "min_spread_dollars": settings.strategy_min_spread_dollars,
        "probability_gap": settings.strategy_probability_gap,
        "order_count": settings.strategy_order_count,
        "limit_price_cents": settings.strategy_limit_price_cents,
        "max_spread_dollars": settings.trade_max_entry_spread_dollars,
    }
    tr, eq, _ = run_rule_backtest(records, strategy_signal_fn=make_bar_strategy_fn(params), paper_cfg=cfg)

    sens = fee_slippage_sensitivity(
        base_trades=tr,
        equity_curve=eq,
        fee_grid=[0, 1, 2, 5],
        slippage_grid=[0, 1, 2],
        contracts_per_trade=[settings.strategy_order_count] * len(tr),
    )
    for s in sens[:12]:
        print(s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Kalshi research / trading bot (not investment advice)")
    sub = p.add_subparsers(dest="command", required=False)

    sub.add_parser("list-markets", help="List open markets")

    sc = sub.add_parser(
        "scan",
        help="Kalshi-only: boxed YES+NO surplus after fees + edge vs TRADE_FAIR_YES_PROB (optional LLM)",
    )
    sc.add_argument("--limit", type=int, default=30, help="Max open markets to pull orderbooks for")
    sc.add_argument(
        "--llm",
        action="store_true",
        help="If TRADE_LLM_SCREEN_ENABLED and OPENAI_API_KEY, ask model for fair_yes per title",
    )

    lt = sub.add_parser(
        "llm-trade",
        help="LLM reasons over open markets; bot re-checks edge/fees; optional execute (OPENAI_API_KEY)",
    )
    lt.add_argument(
        "--execute",
        action="store_true",
        help="Submit orders when LLM+math pass (still needs TRADE_LLM_AUTO_EXECUTE=true; DRY_RUN respected)",
    )
    lt.add_argument(
        "--loop",
        action="store_true",
        help="Run scan+evaluate repeatedly until Ctrl+C (sleep --interval seconds between passes)",
    )
    lt.add_argument(
        "--interval",
        type=float,
        default=120.0,
        metavar="SEC",
        help="Seconds between iterations when --loop (default: 120)",
    )
    lt.add_argument(
        "--web",
        action="store_true",
        help="Start local dashboard with live balance/exposure line chart (sets DASHBOARD_ENABLED)",
    )
    lt.add_argument(
        "--tape",
        action="store_true",
        help="Use public trade-tape ranking first (TRADE_TAPE_*), then LLM — combines with standalone tape-trade",
    )

    dt = sub.add_parser(
        "discover-trade",
        help="LLM filters market titles only; buy/sell from .env rules (TRADE_BUY_* / edge). Multi-ticker.",
    )
    dt.add_argument(
        "--execute",
        action="store_true",
        help="Submit when LLM includes ticker and rules fire (needs TRADE_DISCOVER_AUTO_EXECUTE=true)",
    )
    dt.add_argument("--loop", action="store_true", help="Repeat until Ctrl+C")
    dt.add_argument(
        "--interval",
        type=float,
        default=120.0,
        metavar="SEC",
        help="Seconds between iterations when --loop (default: 120)",
    )
    dt.add_argument("--web", action="store_true", help="Local dashboard (sets DASHBOARD_ENABLED)")

    tt = sub.add_parser(
        "tape-trade",
        help="Rank tickers by recent public trade flow (no user IDs), then .env rules — not true copy-trading",
    )
    tt.add_argument(
        "--execute",
        action="store_true",
        help="Submit when rules fire (needs TRADE_TAPE_AUTO_EXECUTE=true)",
    )
    tt.add_argument("--loop", action="store_true", help="Repeat until Ctrl+C")
    tt.add_argument(
        "--interval",
        type=float,
        default=120.0,
        metavar="SEC",
        help="Seconds between iterations when --loop (default: 120)",
    )
    tt.add_argument("--web", action="store_true", help="Local dashboard (sets DASHBOARD_ENABLED)")

    btc = sub.add_parser(
        "bitcoin-trade",
        help="Crypto (BTC/ETH) spot ref + Kalshi crypto markets — pin ticker or TRADE_CRYPTO_KALSHI_PREFIXES; same rules as tape-trade",
    )
    btc.add_argument(
        "--execute",
        action="store_true",
        help="Submit when rules fire (needs TRADE_BITCOIN_AUTO_EXECUTE=true)",
    )
    btc.add_argument("--loop", action="store_true", help="Repeat until Ctrl+C (use --interval for frequency)")
    btc.add_argument(
        "--interval",
        type=float,
        default=45.0,
        metavar="SEC",
        help="Seconds between iterations when --loop (default: 45; CoinGecko is rate-limited — avoid <15s)",
    )
    btc.add_argument("--web", action="store_true", help="Local dashboard (sets DASHBOARD_ENABLED)")

    w = sub.add_parser("watch-market", help="WebSocket ticker + orderbook stream")
    w.add_argument("ticker")

    po = sub.add_parser("place-test-order", help="One shot: buy 1 YES at STRATEGY_LIMIT_PRICE_CENTS")
    po.add_argument("--ticker", default=None)

    tr = sub.add_parser(
        "trade",
        help="Place one limit order (side/action/count/price) via same risk + execution as run",
    )
    tr.add_argument("ticker", help="Market ticker")
    tr.add_argument("--side", choices=["yes", "no"], default="yes")
    tr.add_argument("--action", choices=["buy", "sell"], default="buy")
    tr.add_argument("--count", type=int, default=1)
    tr.add_argument(
        "--yes-price-cents",
        type=int,
        default=None,
        help="Limit price in cents (default: STRATEGY_LIMIT_PRICE_CENTS)",
    )

    sub.add_parser("cancel-all", help="Cancel all resting orders")

    sa = sub.add_parser(
        "sell-all",
        help="Sell every long YES at best bid (IOC-style); ignores take-profit rules — use with care",
    )
    sa.add_argument(
        "--execute",
        action="store_true",
        help="Submit sells (otherwise print planned liquidations only)",
    )

    ase = sub.add_parser(
        "auto-sell",
        help="Poll best YES bid; when bid >= floor, limit-sell long YES at that bid (set min in CLI or .env)",
    )
    ase.add_argument("ticker", nargs="?", default=None, help="Market (default: TRADE_MARKET_TICKER)")
    ase.add_argument(
        "--min-yes-bid-cents",
        type=int,
        default=None,
        help="Override min best YES bid (cents); else TRADE_TAKE_PROFIT_* / TRADE_EXIT_TAKE_PROFIT_MIN_YES_BID_PCT",
    )
    ase.add_argument("--poll", type=float, default=None, help="Seconds between checks (default: TRADE_TAKE_PROFIT_POLL_SECONDS or 2)")
    ase.add_argument("--max-cycles", type=int, default=0, help="Stop after N polls (0 = unlimited)")
    ase.add_argument("--once", action="store_true", help="Exit after one sell attempt (may still dry-run)")

    exs = sub.add_parser(
        "exit-scan",
        help="Cashout check: summarize all long YES vs take-profit rules (TRADE_EXIT_*); --loop to repeat; optional --execute to sell",
    )
    exs.add_argument(
        "--min-yes-bid-cents",
        type=int,
        default=None,
        help="Override min best YES bid (cents); else TRADE_TAKE_PROFIT_* / TRADE_EXIT_TAKE_PROFIT_MIN_YES_BID_PCT",
    )
    exs.add_argument(
        "--execute",
        action="store_true",
        help="After printing the summary, submit take-profit sells where rules pass (batch auto-sell)",
    )
    exs.add_argument(
        "--loop",
        action="store_true",
        help="Repeat the scan every --interval seconds until Ctrl+C",
    )
    exs.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Seconds between scans when --loop (default: 30; min 5)",
    )

    sb = sub.add_parser(
        "sell-bot",
        help="Parallel exit-only loop: batch auto-sell (same as exit-scan --loop --execute). Does not start the "
        "Flask monitor unless --web. Set TRADE_AUTO_SELL_AFTER_EACH_PASS=false on the trade process.",
    )
    sb.add_argument(
        "--interval",
        type=float,
        default=None,
        metavar="SEC",
        help="Seconds between scans (default: SELL_BOT_INTERVAL_SECONDS from .env, else 30; min 5)",
    )
    sb.add_argument(
        "--min-yes-bid-cents",
        type=int,
        default=None,
        help="Override min best YES bid (cents); else TRADE_EXIT_* / implied-pct rules",
    )
    sb.add_argument(
        "--web",
        action="store_true",
        help="Start local dashboard + portfolio chart (sets DASHBOARD_ENABLED)",
    )
    sb.add_argument(
        "--no-execute",
        action="store_true",
        help="Print cashout summary only (no sells) — same as exit-scan without --execute",
    )

    pw = sub.add_parser(
        "positions-watch",
        help="Long YES only: per-ticker book, taker tape lean (YES vs NO), optional candle drift — read-only",
    )
    pw.add_argument(
        "--loop",
        action="store_true",
        help="Refresh every --interval until Ctrl+C",
    )
    pw.add_argument(
        "--interval",
        type=float,
        default=45.0,
        metavar="SEC",
        help="Seconds between refreshes when --loop (default: 45; min 5)",
    )
    pw.add_argument("--json", action="store_true", help="Print one JSON array per refresh (for scripts)")
    pw.add_argument(
        "--no-candles",
        action="store_true",
        help="Skip REST candlesticks (faster; tape + book only)",
    )
    pw.add_argument(
        "--max-trades",
        type=int,
        default=200,
        metavar="N",
        help="Max public prints per ticker for tape lean (default: 200)",
    )

    run = sub.add_parser("run", help="Run strategy loop (default command); opens local monitor in browser")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--live", action="store_true")
    run.add_argument("--no-web", action="store_true", help="Disable Flask dashboard and browser open")

    bt = sub.add_parser("backtest", help="Run rule backtest on JSONL price records")
    bt.add_argument("data", type=Path, help="Path to JSONL (ts, ticker, yes_bid_dollars, yes_ask_dollars)")

    sw = sub.add_parser("sweep", help="Parameter sweep on JSONL (small grid)")
    sw.add_argument("data", type=Path)

    wf = sub.add_parser("walk-forward", help="Walk-forward / OOS windows on JSONL")
    wf.add_argument("data", type=Path)

    sens = sub.add_parser("sensitivity", help="Fee/slippage stress on JSONL")
    sens.add_argument("data", type=Path)

    exx = sub.add_parser(
        "exit-expectancy",
        help="Break-even avg win vs your closes: parse auto_sell_profit_estimate from structured JSONL (fee-aware)",
    )
    exx.add_argument(
        "--log",
        type=Path,
        default=None,
        help="JSONL path (default: STRUCTURED_LOG_PATH or logs/kalshi_bot.jsonl)",
    )
    exx.add_argument(
        "--max-lines",
        type=int,
        default=200_000,
        help="Parse at most this many tail lines (default: 200000)",
    )

    return p


def main(argv: list[str] | None = None) -> None:
    apply_certifi_ca_bundle()
    load_dotenv(project_root() / ".env")
    argv = argv if argv is not None else sys.argv[1:]
    p = build_parser()
    args = p.parse_args(argv)

    cmd = args.command or "run"

    if cmd == "run":
        if getattr(args, "dry_run", False):
            os.environ["DRY_RUN"] = "true"
        if getattr(args, "live", False):
            os.environ["LIVE_TRADING"] = "true"
            os.environ["DRY_RUN"] = "false"
        if getattr(args, "no_web", False):
            os.environ["DASHBOARD_ENABLED"] = "false"
    if cmd == "llm-trade" and getattr(args, "web", False):
        os.environ["DASHBOARD_ENABLED"] = "true"
    if cmd == "discover-trade" and getattr(args, "web", False):
        os.environ["DASHBOARD_ENABLED"] = "true"
    if cmd == "tape-trade" and getattr(args, "web", False):
        os.environ["DASHBOARD_ENABLED"] = "true"
    if cmd == "bitcoin-trade" and getattr(args, "web", False):
        os.environ["DASHBOARD_ENABLED"] = "true"
    if cmd == "sell-bot" and getattr(args, "web", False):
        os.environ["DASHBOARD_ENABLED"] = "true"

    get_settings.cache_clear()
    settings = get_settings()

    try:
        if cmd == "list-markets":
            cmd_list_markets(settings)
        elif cmd == "scan":
            cmd_scan(settings, limit=args.limit, use_llm=args.llm)
        elif cmd == "llm-trade":
            cmd_llm_trade(
                settings,
                execute=bool(args.execute or settings.trade_llm_cli_execute),
                loop=args.loop,
                interval_seconds=max(5.0, float(args.interval)),
                use_tape=bool(getattr(args, "tape", False) or settings.trade_llm_use_tape_universe),
            )
        elif cmd == "discover-trade":
            cmd_discover_trade(
                settings,
                execute=args.execute,
                loop=args.loop,
                interval_seconds=max(5.0, float(args.interval)),
            )
        elif cmd == "tape-trade":
            cmd_tape_trade(
                settings,
                execute=args.execute,
                loop=args.loop,
                interval_seconds=max(5.0, float(args.interval)),
            )
        elif cmd == "bitcoin-trade":
            cmd_bitcoin_trade(
                settings,
                execute=args.execute,
                loop=args.loop,
                interval_seconds=max(5.0, float(args.interval)),
            )
        elif cmd == "watch-market":
            cmd_watch_market(settings, args.ticker)
        elif cmd == "place-test-order":
            cmd_place_test_order(settings, args.ticker)
        elif cmd == "trade":
            cmd_trade(
                settings,
                ticker=args.ticker,
                side=args.side,
                action=args.action,
                count=args.count,
                yes_price_cents=args.yes_price_cents,
            )
        elif cmd == "positions-watch":
            cmd_positions_watch(
                settings,
                loop=bool(getattr(args, "loop", False)),
                interval_seconds=max(5.0, float(getattr(args, "interval", 45.0))),
                json_mode=bool(getattr(args, "json", False)),
                include_candles=not bool(getattr(args, "no_candles", False)),
                max_trades_per_ticker=int(getattr(args, "max_trades", 200)),
            )
        elif cmd == "exit-scan":
            cmd_exit_scan(
                settings,
                min_yes_bid_cents=getattr(args, "min_yes_bid_cents", None),
                execute=bool(getattr(args, "execute", False)),
                loop=bool(getattr(args, "loop", False)),
                interval_seconds=max(5.0, float(getattr(args, "interval", 30.0))),
            )
        elif cmd == "sell-bot":
            eff_iv = getattr(args, "interval", None)
            if eff_iv is None:
                eff_iv = float(settings.sell_bot_interval_seconds)
            cmd_sell_bot(
                settings,
                min_yes_bid_cents=getattr(args, "min_yes_bid_cents", None),
                interval_seconds=max(5.0, float(eff_iv)),
                execute=not bool(getattr(args, "no_execute", False)),
                web=bool(getattr(args, "web", False)),
            )
        elif cmd == "auto-sell":
            cmd_auto_sell(
                settings,
                ticker=args.ticker,
                min_yes_bid_cents=args.min_yes_bid_cents,
                poll_seconds=args.poll,
                max_cycles=args.max_cycles,
                once=args.once,
            )
        elif cmd == "cancel-all":
            cmd_cancel_all(settings)
        elif cmd == "sell-all":
            cmd_sell_all(settings, execute=bool(getattr(args, "execute", False)))
        elif cmd == "run":
            cmd_run_bot(settings)
        elif cmd == "backtest":
            cmd_backtest(settings, args.data)
        elif cmd == "sweep":
            cmd_sweep(settings, args.data)
        elif cmd == "walk-forward":
            cmd_walk_forward(settings, args.data)
        elif cmd == "sensitivity":
            cmd_sensitivity(settings, args.data)
        elif cmd == "exit-expectancy":
            from kalshi_bot.expectancy_report import run_expectancy_report

            print(
                run_expectancy_report(
                    log_path=getattr(args, "log", None),
                    max_lines=max(1000, int(getattr(args, "max_lines", 200_000))),
                )
            )
        else:
            raise AssertionError(cmd)
    except AuthError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
