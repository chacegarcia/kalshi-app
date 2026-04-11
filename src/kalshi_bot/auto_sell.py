"""REST-driven exits: take-profit by implied YES bid % and/or profit margin vs entry; IOC by default.

Uses Kalshi ``time_in_force`` (e.g. ``immediate_or_cancel``) so exits do not sit as long-lived maker
orders unless you set ``good_till_canceled``. Educational wiring only.
"""

from __future__ import annotations

import time

from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings
from kalshi_bot.execution import DryRunLedger
from kalshi_bot.logger import StructuredLogger
from kalshi_bot.market_data import best_yes_bid_cents, get_orderbook
from kalshi_bot.portfolio import (
    estimate_yes_entry_cents_from_position,
    fetch_portfolio_snapshot,
    get_market_position_row,
)
from kalshi_bot.risk import RiskManager
from kalshi_bot.trading import build_sdk_client, make_limit_intent, trade_execute


def _resolve_entry_reference_yes_cents(
    settings: Settings, client: KalshiSdkClient, ticker: str, log: StructuredLogger
) -> int | None:
    if settings.trade_exit_entry_reference_yes_cents is not None:
        return settings.trade_exit_entry_reference_yes_cents
    if not settings.trade_exit_estimate_entry_from_portfolio:
        return None
    row = get_market_position_row(client, ticker)
    if row is None:
        return None
    est = estimate_yes_entry_cents_from_position(row)
    if est is not None:
        log.info("auto_sell_entry_estimate", ticker=ticker, estimated_yes_entry_cents=est)
    return est


def _should_fire_exit(
    *,
    best_bid_cents: int,
    settings: Settings,
    cli_min_yes_bid_cents: int | None,
    entry_ref_cents: int | None,
) -> tuple[bool, str]:
    """True if we should submit a sell (OR: min implied bid OR profit margin vs entry)."""
    t_min = settings.auto_sell_effective_min_yes_bid_cents(cli_min_yes_bid_cents)
    pct_hit = t_min is not None and best_bid_cents >= t_min

    profit_hit = False
    if settings.trade_exit_min_profit_cents_per_contract is not None and entry_ref_cents is not None:
        need = entry_ref_cents + settings.trade_exit_min_profit_cents_per_contract
        profit_hit = best_bid_cents >= need

    if settings.trade_exit_only_profit_margin:
        if profit_hit:
            return True, "take_profit_profit_margin"
        return False, "wait_profit_only_mode"

    if pct_hit and profit_hit:
        return True, "take_profit_implied_pct_and_margin"
    if pct_hit:
        return True, "take_profit_implied_pct"
    if profit_hit:
        return True, "take_profit_profit_margin"
    return False, "wait"


def run_auto_sell_loop(
    settings: Settings,
    *,
    ticker: str,
    cli_min_yes_bid_cents: int | None,
    poll_seconds: float,
    max_cycles: int,
    stop_after_one_sell: bool,
    log: StructuredLogger,
) -> None:
    if settings.trade_exit_only_profit_margin and settings.trade_exit_min_profit_cents_per_contract is None:
        raise ValueError("TRADE_EXIT_ONLY_PROFIT_MARGIN=true requires TRADE_EXIT_MIN_PROFIT_CENTS_PER_CONTRACT")

    client = build_sdk_client(settings)
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    cycle = 0
    sold_once = False

    while max_cycles == 0 or cycle < max_cycles:
        cycle += 1
        snap = fetch_portfolio_snapshot(client, ticker=ticker)
        signed = snap.positions_by_ticker.get(ticker, 0.0)
        if signed <= 0:
            log.info("auto_sell_skip", reason="no_long_yes", ticker=ticker, signed=signed)
            if stop_after_one_sell and sold_once:
                return
            time.sleep(poll_seconds)
            continue

        entry_ref = _resolve_entry_reference_yes_cents(settings, client, ticker, log)
        if (
            settings.trade_exit_min_profit_cents_per_contract is not None
            and entry_ref is None
            and not settings.trade_exit_only_profit_margin
        ):
            log.warning(
                "auto_sell_no_entry_reference",
                ticker=ticker,
                hint="set TRADE_EXIT_ENTRY_REFERENCE_YES_CENTS or enable TRADE_EXIT_ESTIMATE_ENTRY_FROM_PORTFOLIO",
            )

        ob = get_orderbook(client, ticker)
        best = best_yes_bid_cents(ob)
        if best is None:
            log.info("auto_sell_skip", reason="no_yes_bids", ticker=ticker)
            time.sleep(poll_seconds)
            continue

        fire, reason = _should_fire_exit(
            best_bid_cents=best,
            settings=settings,
            cli_min_yes_bid_cents=cli_min_yes_bid_cents,
            entry_ref_cents=entry_ref,
        )
        if not fire:
            eff = settings.auto_sell_effective_min_yes_bid_cents(cli_min_yes_bid_cents)
            log.info(
                "auto_sell_wait",
                ticker=ticker,
                best_yes_bid_cents=best,
                effective_min_yes_bid_cents=eff,
                entry_ref_yes_cents=entry_ref,
                detail=reason,
            )
            time.sleep(poll_seconds)
            continue

        count = min(int(signed), settings.max_contracts_per_market)
        if count < 1:
            time.sleep(poll_seconds)
            continue

        limit_cents = max(1, best - settings.trade_exit_sell_aggression_cents)
        tif = settings.trade_exit_sell_time_in_force

        intent = make_limit_intent(
            ticker=ticker,
            side="yes",
            action="sell",
            count=count,
            yes_price_cents=limit_cents,
            time_in_force=tif,
        )
        log.info(
            "auto_sell_fire",
            ticker=ticker,
            count=count,
            limit_yes_price_cents=limit_cents,
            best_yes_bid_cents=best,
            time_in_force=tif,
            trigger=reason,
            aggression_cents=settings.trade_exit_sell_aggression_cents,
        )
        trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
        sold_once = True
        if stop_after_one_sell:
            return
        time.sleep(poll_seconds)
