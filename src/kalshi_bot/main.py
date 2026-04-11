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

from kalshi_bot.auth import AuthError, build_kalshi_auth
from kalshi_bot.backtest import load_price_records_jsonl, parameter_sweep, run_rule_backtest, walk_forward_eval
from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings, get_settings
from kalshi_bot.execution import (
    DryRunLedger,
    cancel_all_resting_orders,
    cancel_stale_orders,
    execute_intent,
)
from kalshi_bot.logger import get_logger
from kalshi_bot.market_data import list_open_markets, summarize_market_row
from kalshi_bot.metrics import NO_GUARANTEE_DISCLAIMER, fee_slippage_sensitivity
from kalshi_bot.paper_engine import PaperFillConfig
from kalshi_bot.portfolio import fetch_portfolio_snapshot
from kalshi_bot.risk import RiskManager
from kalshi_bot.strategy import SampleSpreadGapStrategy, TradeIntent, make_bar_strategy_fn
from kalshi_bot.monitor import heartbeat, start_dashboard
from kalshi_bot.ws import KalshiWS


def _sdk_client(settings: Settings) -> KalshiSdkClient:
    auth = build_kalshi_auth(
        settings.kalshi_api_key_id,
        key_path=settings.kalshi_private_key_path,
        key_pem=settings.kalshi_private_key_pem,
    )
    return KalshiSdkClient(rest_base_url=settings.rest_base_url, auth=auth)


def cmd_list_markets(settings: Settings) -> None:
    client = _sdk_client(settings)
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
        print("Set STRATEGY_MARKET_TICKER or pass --ticker", file=sys.stderr)
        sys.exit(2)
    client = _sdk_client(settings)
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    intent = TradeIntent(
        ticker=t,
        side="yes",
        action="buy",
        count=1,
        yes_price_cents=settings.strategy_limit_price_cents,
    )
    execute_intent(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)


def cmd_cancel_all(settings: Settings) -> None:
    client = _sdk_client(settings)
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    n = cancel_all_resting_orders(client, log)
    print(f"Requested cancel for {n} orders.")


def cmd_run_bot(settings: Settings) -> None:
    if not settings.strategy_market_ticker:
        print("STRATEGY_MARKET_TICKER is required for run", file=sys.stderr)
        sys.exit(2)

    start_dashboard(settings)

    auth = build_kalshi_auth(
        settings.kalshi_api_key_id,
        key_path=settings.kalshi_private_key_path,
        key_pem=settings.kalshi_private_key_pem,
    )
    client = _sdk_client(settings)
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    strategy = SampleSpreadGapStrategy(settings)
    last_signal = 0.0

    async def maintenance_loop() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                snap = fetch_portfolio_snapshot(client, ticker=settings.strategy_market_ticker)
                risk.record_balance_sample(snap.balance_cents)
                cancel_stale_orders(client, settings, log)
                if settings.dashboard_enabled:
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

    w = sub.add_parser("watch-market", help="WebSocket ticker + orderbook stream")
    w.add_argument("ticker")

    po = sub.add_parser("place-test-order", help="One shot through risk + execution")
    po.add_argument("--ticker", default=None)

    sub.add_parser("cancel-all", help="Cancel all resting orders")

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
    load_dotenv()
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

    get_settings.cache_clear()
    settings = get_settings()

    try:
        if cmd == "list-markets":
            cmd_list_markets(settings)
        elif cmd == "watch-market":
            cmd_watch_market(settings, args.ticker)
        elif cmd == "place-test-order":
            cmd_place_test_order(settings, args.ticker)
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
