"""CLI: live trading loop, WebSocket watch, backtest, sweep, walk-forward."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from kalshi_bot.client import KalshiSdkClient

from kalshi_bot.ssl_bundle import apply_certifi_ca_bundle
from kalshi_bot.auth import AuthError, build_kalshi_auth
from kalshi_bot.auto_sell import auto_sell_scan_all_long_yes, run_auto_sell_loop
from kalshi_bot.backtest import load_price_records_jsonl, parameter_sweep, run_rule_backtest, walk_forward_eval
from kalshi_bot.config import Settings, get_settings, project_root
from kalshi_bot.execution import (
    DryRunLedger,
    cancel_all_resting_orders,
    cancel_stale_orders,
    execute_intent,
)
from kalshi_bot.logger import StructuredLogger, get_logger
from kalshi_bot.market_data import list_open_markets, summarize_market_row
from kalshi_bot.metrics import NO_GUARANTEE_DISCLAIMER, fee_slippage_sensitivity
from kalshi_bot.scanner import format_scan_report, scan_kalshi_opportunities
from kalshi_bot.paper_engine import PaperFillConfig
from kalshi_bot.portfolio import fetch_portfolio_snapshot, print_portfolio_balance_line
from kalshi_bot.risk import RiskManager
from kalshi_bot.strategy import SampleSpreadGapStrategy, make_bar_strategy_fn
from kalshi_bot.discover_runner import run_discover_rule_pipeline
from kalshi_bot.tape_runner import run_tape_rule_pipeline
from kalshi_bot.monitor import heartbeat, record_portfolio_series_point, start_dashboard
from kalshi_bot.trading import build_sdk_client, make_limit_intent, trade_execute
from kalshi_bot.ws import KalshiWS


def _client_for_balance(settings: Settings, dash_client: KalshiSdkClient | None) -> KalshiSdkClient:
    """Reuse dashboard client or build one so balance prints even without --web."""
    return dash_client if dash_client is not None else build_sdk_client(settings)


def _maybe_exit_scan_after_pass(settings: Settings, client: KalshiSdkClient, log: StructuredLogger) -> None:
    """After a trading pass summary, optionally scan all long YES and submit take-profit sells (TRADE_EXIT_*)."""
    if not settings.trade_auto_sell_after_each_pass:
        return
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
    from kalshi_bot.llm_runner import run_llm_opportunity_pipeline

    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)

    dash_client = None
    if settings.dashboard_enabled:
        start_dashboard(settings)
        dash_client = build_sdk_client(settings)
        try:
            snap0 = fetch_portfolio_snapshot(dash_client, ticker=None)
            record_portfolio_series_point(snap0.balance_cents, float(snap0.total_exposure_cents))
        except Exception:
            pass

    iteration = 0
    try:
        while True:
            iteration += 1
            if loop:
                print(f"--- llm-trade iteration {iteration} ---", flush=True)
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
            print(f"Orders submitted this run (0 = none or scan-only): {n}")
            for line in run_stats.lines():
                print(line, flush=True)
            bal_client = _client_for_balance(settings, dash_client)
            print_portfolio_balance_line(bal_client)
            _maybe_exit_scan_after_pass(settings, bal_client, log)
            if dash_client is not None:
                try:
                    snap = fetch_portfolio_snapshot(dash_client, ticker=None)
                    record_portfolio_series_point(snap.balance_cents, float(snap.total_exposure_cents))
                except Exception:
                    pass
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
        try:
            snap0 = fetch_portfolio_snapshot(dash_client, ticker=None)
            record_portfolio_series_point(snap0.balance_cents, float(snap0.total_exposure_cents))
        except Exception:
            pass

    iteration = 0
    try:
        while True:
            iteration += 1
            if loop:
                print(f"--- discover-trade iteration {iteration} ---", flush=True)
            try:
                n, run_stats = run_discover_rule_pipeline(settings, execute=execute, log=log)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(2)
            print(f"Orders submitted this run (0 = none): {n}")
            for line in run_stats.lines():
                print(line, flush=True)
            bal_client = _client_for_balance(settings, dash_client)
            print_portfolio_balance_line(bal_client)
            _maybe_exit_scan_after_pass(settings, bal_client, log)
            if dash_client is not None:
                try:
                    snap = fetch_portfolio_snapshot(dash_client, ticker=None)
                    record_portfolio_series_point(snap.balance_cents, float(snap.total_exposure_cents))
                except Exception:
                    pass
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
        try:
            snap0 = fetch_portfolio_snapshot(dash_client, ticker=None)
            record_portfolio_series_point(snap0.balance_cents, float(snap0.total_exposure_cents))
        except Exception:
            pass

    iteration = 0
    try:
        while True:
            iteration += 1
            if loop:
                print(f"--- tape-trade iteration {iteration} ---", flush=True)
            try:
                n, run_stats = run_tape_rule_pipeline(settings, execute=execute, log=log)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(2)
            print(f"Orders submitted this run (0 = none): {n}")
            for line in run_stats.lines():
                print(line, flush=True)
            bal_client = _client_for_balance(settings, dash_client)
            print_portfolio_balance_line(bal_client)
            _maybe_exit_scan_after_pass(settings, bal_client, log)
            if dash_client is not None:
                try:
                    snap = fetch_portfolio_snapshot(dash_client, ticker=None)
                    record_portfolio_series_point(snap.balance_cents, float(snap.total_exposure_cents))
                except Exception:
                    pass
            if not loop:
                break
            print(f"Sleeping {interval_seconds:.0f}s… (Ctrl+C to stop)\n", flush=True)
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("\ntape-trade loop stopped.", file=sys.stderr)


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
        min_profit_cents=settings.trade_exit_min_profit_cents_per_contract,
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
            record_portfolio_series_point(snap0.balance_cents, float(snap0.total_exposure_cents))
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
                    record_portfolio_series_point(snap.balance_cents, float(snap.total_exposure_cents))
                    heartbeat(
                        f"balance_cents={snap.balance_cents} exposure_cents={snap.total_exposure_cents:.0f}"
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
        "max_yes_ask_dollars": settings.strategy_max_yes_ask_dollars,
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
        "max_yes_ask_dollars": settings.strategy_max_yes_ask_dollars,
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
        "max_yes_ask_dollars": settings.strategy_max_yes_ask_dollars,
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
                execute=args.execute,
                loop=args.loop,
                interval_seconds=max(5.0, float(args.interval)),
                use_tape=getattr(args, "tape", False),
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
        else:
            raise AssertionError(cmd)
    except AuthError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
