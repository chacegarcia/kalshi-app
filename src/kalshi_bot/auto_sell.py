"""REST-driven exits: take-profit, optional stop-loss vs entry, IOC by default.

Uses Kalshi ``time_in_force`` (e.g. ``immediate_or_cancel``) so exits do not sit as long-lived maker
orders unless you set ``good_till_canceled``. Educational wiring only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

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
from kalshi_bot.monitor import notify_auto_sell_outcome
from kalshi_bot.risk import RiskManager
from kalshi_bot.trading import build_sdk_client, make_limit_intent, trade_execute
from kalshi_bot.trading_model import gross_pnl_cents_from_price_move


@dataclass(frozen=True)
class EntryReference:
    """Where ``cents`` came from for exit math (manual env vs portfolio API estimate)."""

    cents: int | None
    source: Literal["manual", "portfolio", "none"]


@dataclass
class ExitScanRow:
    """One long-YES position and how it compares to auto-sell / take-profit rules (cashout check)."""

    ticker: str
    long_yes_shares: float
    best_yes_bid_cents: int | None
    entry_yes_cents: int | None
    effective_min_yes_bid_cents: int | None
    min_bid_for_profit_rule_cents: int | None
    would_take_profit: bool
    detail: str


def collect_exit_scan_rows(
    client: KalshiSdkClient,
    settings: Settings,
    *,
    cli_min_yes_bid_cents: int | None,
    log: StructuredLogger,
) -> list[ExitScanRow]:
    """Read-only: for every long YES, compare book + entry to TRADE_EXIT_* / TRADE_TAKE_PROFIT_* (no orders)."""
    snap = fetch_portfolio_snapshot(client, ticker=None)
    tickers = sorted(t for t, s in snap.positions_by_ticker.items() if s > 0)
    rows: list[ExitScanRow] = []
    eff_floor = settings.auto_sell_effective_min_yes_bid_cents(cli_min_yes_bid_cents)
    mpc = settings.trade_exit_effective_min_profit_cents_per_contract
    for ticker in tickers:
        signed = float(snap.positions_by_ticker.get(ticker, 0.0))
        entry_ref = _resolve_entry_reference(settings, client, ticker, log)
        min_profit_bid: int | None = None
        if mpc is not None and entry_ref.cents is not None:
            min_profit_bid = int(entry_ref.cents + mpc)

        ob = get_orderbook(client, ticker)
        best = best_yes_bid_cents(ob)
        if best is None:
            rows.append(
                ExitScanRow(
                    ticker=ticker,
                    long_yes_shares=signed,
                    best_yes_bid_cents=None,
                    entry_yes_cents=entry_ref.cents,
                    effective_min_yes_bid_cents=eff_floor,
                    min_bid_for_profit_rule_cents=min_profit_bid,
                    would_take_profit=False,
                    detail="no_yes_bids",
                )
            )
            continue

        fire, reason = _should_fire_exit(
            best_bid_cents=best,
            settings=settings,
            cli_min_yes_bid_cents=cli_min_yes_bid_cents,
            entry_ref_cents=entry_ref.cents,
            entry_source=entry_ref.source,
        )
        rows.append(
            ExitScanRow(
                ticker=ticker,
                long_yes_shares=signed,
                best_yes_bid_cents=best,
                entry_yes_cents=entry_ref.cents,
                effective_min_yes_bid_cents=eff_floor,
                min_bid_for_profit_rule_cents=min_profit_bid,
                would_take_profit=fire,
                detail=reason,
            )
        )
    return rows


def format_exit_scan_summary(rows: list[ExitScanRow]) -> list[str]:
    """Human-readable lines for the terminal (table + totals)."""
    if not rows:
        return [
            "--- exit-scan (cashout check) ---",
            "  No long YES positions in portfolio.",
            "---",
        ]
    lines = [
        "--- exit-scan (cashout check) ---",
        "  Rules: stop-loss vs entry, TRADE_EXIT_* / TRADE_TAKE_PROFIT_* / TRADE_EXIT_ONLY_PROFIT_MARGIN (same as auto-sell).",
        f"  {'ticker':<28} {'shares':>6} {'bid¢':>5} {'entry¢':>7} {'floor¢':>6} {'profit≥¢':>8}  would_exit  detail",
    ]
    ready = 0
    for r in rows:
        if r.would_take_profit:
            ready += 1
        b = "—" if r.best_yes_bid_cents is None else str(r.best_yes_bid_cents)
        e = "—" if r.entry_yes_cents is None else str(r.entry_yes_cents)
        f = "—" if r.effective_min_yes_bid_cents is None else str(r.effective_min_yes_bid_cents)
        p = "—" if r.min_bid_for_profit_rule_cents is None else str(r.min_bid_for_profit_rule_cents)
        w = "yes" if r.would_take_profit else "no"
        lines.append(
            f"  {r.ticker:<28} {r.long_yes_shares:>6.1f} {b:>5} {e:>7} {f:>6} {p:>8}  {w:<10}  {r.detail}"
        )
    lines.append("---")
    lines.append(f"  Positions: {len(rows)}  |  Would exit now (stop or TP): {ready}")
    lines.append("---")
    return lines


def _resolve_entry_reference(
    settings: Settings, client: KalshiSdkClient, ticker: str, log: StructuredLogger
) -> EntryReference:
    if settings.trade_exit_entry_reference_yes_cents is not None:
        return EntryReference(settings.trade_exit_entry_reference_yes_cents, "manual")
    if not settings.trade_exit_estimate_entry_from_portfolio:
        return EntryReference(None, "none")
    row = get_market_position_row(client, ticker)
    if row is None:
        return EntryReference(None, "none")
    est = estimate_yes_entry_cents_from_position(row)
    if est is not None:
        log.info("auto_sell_entry_estimate", ticker=ticker, estimated_yes_entry_cents=est)
        return EntryReference(est, "portfolio")
    return EntryReference(None, "none")


def _should_fire_exit(
    *,
    best_bid_cents: int,
    settings: Settings,
    cli_min_yes_bid_cents: int | None,
    entry_ref_cents: int | None,
    entry_source: Literal["manual", "portfolio", "none"] = "none",
) -> tuple[bool, str]:
    """True if we should submit a sell (stop-loss vs entry, then take-profit rules).

    Stop-loss math (long YES): ``stop_floor = round(entry_ref_cents * fraction)``, clamped 1–99.
    Fire when ``best_bid_cents <= stop_floor``. So fraction=0.5 means “bid at or below half of *estimated*
    entry in cents” (e.g. entry 40¢ → floor 20¢). This is not the same as “−50% return”; P/L vs entry
    still depends on fill. Entry comes from manual ``TRADE_EXIT_ENTRY_REFERENCE_YES_CENTS`` or
    ``estimate_yes_entry_cents_from_position`` (API-derived; can be wrong—see portfolio helper).

    When ``TRADE_EXIT_STOP_LOSS_SKIP_SUSPECT_PORTFOLIO_ESTIMATE`` is true, portfolio-derived entries
    at ≤5¢ or ≥95¢ are not used for stop-loss (avoids false exits from bad averages like ~99¢).
    """
    skipped_suspect_stop = False
    would_have_stop_floor: int | None = None
    if (
        settings.trade_exit_stop_loss_enabled
        and entry_ref_cents is not None
        and 1 <= entry_ref_cents <= 99
    ):
        frac = float(settings.trade_exit_stop_loss_entry_fraction)
        stop_floor = max(1, min(99, int(round(entry_ref_cents * frac))))
        would_have_stop_floor = stop_floor
        skip_suspect = (
            settings.trade_exit_stop_loss_skip_suspect_portfolio_estimate
            and entry_source == "portfolio"
            and (entry_ref_cents >= 95 or entry_ref_cents <= 5)
        )
        if skip_suspect:
            skipped_suspect_stop = True
        elif best_bid_cents <= stop_floor:
            return True, "stop_loss_entry_fraction"

    t_min = settings.auto_sell_effective_min_yes_bid_cents(cli_min_yes_bid_cents)
    pct_hit = t_min is not None and best_bid_cents >= t_min

    profit_hit = False
    mpc = settings.trade_exit_effective_min_profit_cents_per_contract
    if mpc is not None and entry_ref_cents is not None:
        need = entry_ref_cents + mpc
        profit_hit = best_bid_cents >= need

    if settings.trade_exit_only_profit_margin:
        if profit_hit:
            return True, "take_profit_profit_margin"
        if (
            skipped_suspect_stop
            and would_have_stop_floor is not None
            and best_bid_cents <= would_have_stop_floor
        ):
            return False, "wait_stop_loss_skipped_suspect_portfolio_entry"
        return False, "wait_profit_only_mode"

    if pct_hit and profit_hit:
        return True, "take_profit_implied_pct_and_margin"
    if pct_hit:
        return True, "take_profit_implied_pct"
    if profit_hit:
        return True, "take_profit_profit_margin"
    if (
        skipped_suspect_stop
        and would_have_stop_floor is not None
        and best_bid_cents <= would_have_stop_floor
    ):
        return False, "wait_stop_loss_skipped_suspect_portfolio_entry"
    return False, "wait"


def _format_auto_sell_profit_line(
    *,
    ticker: str,
    count: int,
    limit_cents: int,
    entry_ref: int | None,
    exit_reason: str = "",
) -> str:
    """One-line summary for stdout / exit-scan (gross vs estimated entry; not exchange fill or fees)."""
    proceeds_cents = limit_cents * count
    kind = ""
    if exit_reason.startswith("stop_loss"):
        kind = "stop-loss "
    elif exit_reason.startswith("take_profit"):
        kind = "take-profit "
    if entry_ref is not None:
        gross_cents = gross_pnl_cents_from_price_move(
            shares=count, exit_price_cents=limit_cents, entry_price_cents=entry_ref
        )
        return (
            f"{ticker}: {kind}est. gross P/L ${gross_cents / 100.0:.2f} "
            f"({count} sh × (sell {limit_cents}¢ − entry ~{entry_ref}¢); proceeds ~${proceeds_cents / 100.0:.2f}; before fees)"
        )
    return (
        f"{ticker}: sell {count} sh YES @ {limit_cents}¢ limit — proceeds ~${proceeds_cents / 100.0:.2f} "
        "(P/L unknown: set TRADE_EXIT_ENTRY_REFERENCE_YES_CENTS or TRADE_EXIT_ESTIMATE_ENTRY_FROM_PORTFOLIO=true)"
    )


def try_auto_sell_exit_for_ticker(
    client: KalshiSdkClient,
    settings: Settings,
    risk: RiskManager,
    ledger: DryRunLedger | None,
    ticker: str,
    *,
    cli_min_yes_bid_cents: int | None,
    log: StructuredLogger,
    log_waits: bool = False,
) -> tuple[str, str | None, str | None]:
    """One auto-sell attempt for ``ticker`` (stop-loss or take-profit).

    Returns ``(tag, summary_line, exit_reason)``. ``summary_line`` and ``exit_reason`` are set when tag
    is ``sold`` (proceeds / P/L text and trigger reason, e.g. ``stop_loss_entry_fraction``).
    """
    snap = fetch_portfolio_snapshot(client, ticker=ticker)
    signed = snap.positions_by_ticker.get(ticker, 0.0)
    if signed <= 0:
        if log_waits:
            log.info("auto_sell_skip", reason="no_long_yes", ticker=ticker, signed=signed)
        return "no_long_yes", None, None

    entry_ref = _resolve_entry_reference(settings, client, ticker, log)
    if settings.trade_exit_effective_min_profit_cents_per_contract is not None and entry_ref.cents is None:
        log.warning(
            "auto_sell_no_entry_reference",
            ticker=ticker,
            hint="set TRADE_EXIT_ENTRY_REFERENCE_YES_CENTS or enable TRADE_EXIT_ESTIMATE_ENTRY_FROM_PORTFOLIO",
        )

    ob = get_orderbook(client, ticker)
    best = best_yes_bid_cents(ob)
    if best is None:
        if log_waits:
            log.info("auto_sell_skip", reason="no_yes_bids", ticker=ticker)
        return "no_yes_bids", None, None

    fire, reason = _should_fire_exit(
        best_bid_cents=best,
        settings=settings,
        cli_min_yes_bid_cents=cli_min_yes_bid_cents,
        entry_ref_cents=entry_ref.cents,
        entry_source=entry_ref.source,
    )
    if not fire:
        if log_waits:
            eff = settings.auto_sell_effective_min_yes_bid_cents(cli_min_yes_bid_cents)
            log.info(
                "auto_sell_wait",
                ticker=ticker,
                best_yes_bid_cents=best,
                effective_min_yes_bid_cents=eff,
                entry_ref_yes_cents=entry_ref.cents,
                detail=reason,
            )
        return "wait", None, None

    count = min(int(signed), settings.max_contracts_per_market)
    if count < 1:
        return "zero_contracts", None, None

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
    fire_extra: dict[str, object] = {}
    if reason == "stop_loss_entry_fraction" and entry_ref.cents is not None:
        frac = float(settings.trade_exit_stop_loss_entry_fraction)
        fire_extra["stop_loss_floor_yes_bid_cents"] = max(1, min(99, int(round(entry_ref.cents * frac))))
        fire_extra["stop_loss_entry_fraction"] = frac
    log.info(
        "auto_sell_fire",
        ticker=ticker,
        count=count,
        limit_yes_price_cents=limit_cents,
        best_yes_bid_cents=best,
        time_in_force=tif,
        trigger=reason,
        aggression_cents=settings.trade_exit_sell_aggression_cents,
        **fire_extra,
    )
    trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)

    proceeds_cents = limit_cents * count
    gross_cents: int | None = None
    if entry_ref.cents is not None:
        gross_cents = gross_pnl_cents_from_price_move(
            shares=count, exit_price_cents=limit_cents, entry_price_cents=entry_ref.cents
        )
    log.info(
        "auto_sell_profit_estimate",
        ticker=ticker,
        count=count,
        shares=count,
        limit_yes_price_cents=limit_cents,
        entry_yes_cents=entry_ref.cents,
        proceeds_cents=proceeds_cents,
        estimated_gross_profit_cents=gross_cents,
        note="vs portfolio entry estimate; excludes fees; IOC may partially fill",
    )
    notify_auto_sell_outcome(
        settings,
        gross_profit_cents=gross_cents,
        exit_reason=reason,
        event_payload={
            "ticker": ticker,
            "count": count,
            "shares": count,
            "limit_yes_price_cents": limit_cents,
            "entry_yes_cents": entry_ref.cents,
            "proceeds_cents": proceeds_cents,
            "estimated_gross_profit_cents": gross_cents,
            "note": "vs portfolio entry estimate; excludes fees; IOC may partially fill",
        },
    )
    summary = _format_auto_sell_profit_line(
        ticker=ticker,
        count=count,
        limit_cents=limit_cents,
        entry_ref=entry_ref.cents,
        exit_reason=reason,
    )
    if log_waits:
        print(f"auto-sell: {summary}", flush=True)
    return "sold", summary, reason


def auto_sell_scan_all_long_yes(
    client: KalshiSdkClient,
    settings: Settings,
    *,
    cli_min_yes_bid_cents: int | None,
    log: StructuredLogger,
) -> tuple[int, list[str]]:
    """For each market with a long YES position, run one take-profit check (same rules as ``auto-sell``).

    Returns ``(sell_count, human_lines)`` for terminal summary.
    """
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    snap = fetch_portfolio_snapshot(client, ticker=None)
    tickers = sorted(t for t, s in snap.positions_by_ticker.items() if s > 0)
    sold = 0
    lines: list[str] = []
    for ticker in tickers:
        tag, summary, exit_reason = try_auto_sell_exit_for_ticker(
            client,
            settings,
            risk,
            ledger,
            ticker,
            cli_min_yes_bid_cents=cli_min_yes_bid_cents,
            log=log,
            log_waits=False,
        )
        if tag == "sold":
            sold += 1
            label = (
                "stop-loss"
                if exit_reason == "stop_loss_entry_fraction"
                else "take-profit"
            )
            lines.append(f"exit-scan: {label} sell — {summary}")
    return sold, lines


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
    client = build_sdk_client(settings)
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    cycle = 0
    sold_once = False

    while max_cycles == 0 or cycle < max_cycles:
        cycle += 1
        tag, _summary, _reason = try_auto_sell_exit_for_ticker(
            client,
            settings,
            risk,
            ledger,
            ticker,
            cli_min_yes_bid_cents=cli_min_yes_bid_cents,
            log=log,
            log_waits=True,
        )
        if tag == "no_long_yes":
            if stop_after_one_sell and sold_once:
                return
            time.sleep(poll_seconds)
            continue
        if tag == "sold":
            sold_once = True
            if stop_after_one_sell:
                return
        if tag in ("no_yes_bids", "wait", "zero_contracts"):
            pass
        time.sleep(poll_seconds)
