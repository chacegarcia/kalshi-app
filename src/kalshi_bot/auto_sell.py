"""REST-driven exits: take-profit, optional stop-loss vs entry, IOC by default.

Uses Kalshi ``time_in_force`` (e.g. ``immediate_or_cancel``) so exits do not sit as long-lived maker
orders unless you set ``good_till_canceled``. Educational wiring only.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

# High-water best YES bid per ticker for trailing exits (in-process only; resets when flat).
_PEAK_LOCK = threading.Lock()
_PEAK_YES_BID_CENTS: dict[str, int] = {}
# Last stop-loss rebuy time per ticker (cooldown for TRADE_REBUY_AFTER_STOP_LOSS_*).
_REBUY_COOLDOWN_LOCK = threading.Lock()
_REBUY_LAST_UNIX: dict[str, float] = {}


def _clear_peak_yes_bid(ticker: str) -> None:
    with _PEAK_LOCK:
        _PEAK_YES_BID_CENTS.pop(ticker, None)


def _update_peak_yes_bid(ticker: str, best_bid_cents: int | None) -> int | None:
    """Update session peak best YES bid; return current peak."""
    if best_bid_cents is None:
        with _PEAK_LOCK:
            return _PEAK_YES_BID_CENTS.get(ticker)
    with _PEAK_LOCK:
        prev = _PEAK_YES_BID_CENTS.get(ticker, int(best_bid_cents))
        peak = max(int(prev), int(best_bid_cents))
        _PEAK_YES_BID_CENTS[ticker] = peak
        return peak


def implied_yes_chance_cents_from_orderbook(ob: Any, best_yes_bid_cents: int) -> int:
    """Kalshi-style YES probability in ¢: mid(best YES bid, implied lift YES ask) when the book allows; else bid."""
    _yb, nb = yes_bid_and_no_bid_cents_for_trading(ob)
    if nb is None:
        return max(1, min(99, int(best_yes_bid_cents)))
    ya_d = implied_yes_ask_dollars(nb / 100.0)
    ya_c = int(max(1, min(99, round(ya_d * 100.0))))
    mid = int(round((float(best_yes_bid_cents) + float(ya_c)) / 2.0))
    return max(1, min(99, mid))

from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings
from kalshi_bot.edge_math import implied_yes_ask_dollars
from kalshi_bot.execution import DryRunLedger
from kalshi_bot.logger import StructuredLogger
from kalshi_bot.market_data import (
    best_yes_bid_cents,
    fetch_public_trades_for_ticker,
    get_orderbook,
    lift_yes_ask_cents_from_orderbook,
    market_category_for_ticker,
    market_title_for_ticker,
    summarize_taker_tape_lean,
    yes_bid_and_no_bid_cents_for_trading,
)
from kalshi_bot.portfolio import (
    estimate_yes_entry_cents_from_position,
    fetch_portfolio_snapshot,
    get_market_position_row,
)
from kalshi_bot.monitor import notify_auto_sell_outcome
from kalshi_bot.risk import RiskManager
from kalshi_bot.strategy import should_skip_buy_ticker_substrings, skip_buy_yes_longshot
from kalshi_bot.trading import build_sdk_client, make_limit_intent, trade_execute
from kalshi_bot.trading_model import gross_pnl_cents_from_price_move


def _tape_relaxed_min_profit_cents_effective(
    client: KalshiSdkClient,
    settings: Settings,
    ticker: str,
    entry_ref_cents: int | None,
    log: StructuredLogger,
) -> int | None:
    """Lower min-profit take-profit threshold when tape is NO-heavy (optional). Returns override cents or None."""
    relax = float(settings.trade_exit_tape_no_heavy_relax_min_profit_cents)
    if relax <= 0.0:
        return None
    mpc_base = settings.trade_exit_min_profit_cents_for_entry(entry_ref_cents)
    if mpc_base is None or entry_ref_cents is None:
        return None
    try:
        raw = fetch_public_trades_for_ticker(
            client, ticker, max_trades=settings.trade_exit_tape_lookback_max_trades
        )
        lean = summarize_taker_tape_lean(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("exit_tape_lean_fetch_fail", ticker=ticker, error=str(exc))
        return None
    if lean.trade_count < settings.trade_exit_tape_min_trades_for_exit:
        return None
    if lean.yes_share is None:
        return None
    if lean.yes_share > float(settings.trade_exit_tape_no_heavy_max_yes_share):
        return None
    eff = max(0, int(round(float(mpc_base) - relax)))
    log.info(
        "exit_tape_no_heavy_relax_applied",
        ticker=ticker,
        mpc_base=mpc_base,
        mpc_effective=eff,
        yes_share=lean.yes_share,
        tape_trades=lean.trade_count,
    )
    return eff


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
    for ticker in tickers:
        signed = float(snap.positions_by_ticker.get(ticker, 0.0))
        entry_ref = _resolve_entry_reference(settings, client, ticker, log)
        mpc = settings.trade_exit_min_profit_cents_for_entry(entry_ref.cents)
        min_profit_bid: int | None = None
        if mpc is not None and entry_ref.cents is not None:
            min_profit_bid = int(math.ceil(float(entry_ref.cents) + mpc - 1e-9))

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

        hmin = settings.trade_exit_hold_to_settlement_min_chance_cents
        sell_within = settings.trade_exit_sell_within_cents_of_max_payout
        rel = settings.trade_exit_min_profit_cents_when_no_full_payout_indication
        ind = settings.trade_exit_full_payout_indication_min_chance_cents
        chance: int | None = None
        if hmin > 0 or sell_within > 0 or (rel > 0 and ind > 0):
            chance = implied_yes_chance_cents_from_orderbook(ob, best)
        near_max = (
            sell_within > 0
            and chance is not None
            and chance >= (100 - sell_within)
        )
        peak = _update_peak_yes_bid(ticker, best)
        min_eff = _tape_relaxed_min_profit_cents_effective(
            client, settings, ticker, entry_ref.cents, log
        )
        fire, reason = _should_fire_exit(
            best_bid_cents=best,
            settings=settings,
            cli_min_yes_bid_cents=cli_min_yes_bid_cents,
            entry_ref_cents=entry_ref.cents,
            entry_source=entry_ref.source,
            peak_bid_cents=peak,
            implied_yes_chance_cents=chance,
            min_profit_cents_effective=min_eff,
        )
        if not fire and near_max:
            fire, reason = True, "take_profit_near_max_payout"
        in_hold = not near_max and hmin > 0 and chance is not None and chance >= hmin
        if in_hold and not (fire and _exit_bypasses_hold_to_settlement(reason)):
            rows.append(
                ExitScanRow(
                    ticker=ticker,
                    long_yes_shares=signed,
                    best_yes_bid_cents=best,
                    entry_yes_cents=entry_ref.cents,
                    effective_min_yes_bid_cents=eff_floor,
                    min_bid_for_profit_rule_cents=min_profit_bid,
                    would_take_profit=False,
                    detail=f"hold_to_settlement_chance_{chance}_min_{hmin}",
                )
            )
            continue

        detail = reason
        if min_eff is not None:
            detail = f"{reason} (tape no-heavy: relaxed min profit)"
        rows.append(
            ExitScanRow(
                ticker=ticker,
                long_yes_shares=signed,
                best_yes_bid_cents=best,
                entry_yes_cents=entry_ref.cents,
                effective_min_yes_bid_cents=eff_floor,
                min_bid_for_profit_rule_cents=min_profit_bid,
                would_take_profit=fire,
                detail=detail,
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
        "  Rules (same as auto-sell): hold to settlement when chance ≥ TRADE_EXIT_HOLD_TO_SETTLEMENT_MIN_CHANCE_CENTS (unless near-max sell). When chance < TRADE_EXIT_FULL_PAYOUT_INDICATION_MIN_CHANCE_CENTS, may TP at entry + TRADE_EXIT_MIN_PROFIT_CENTS_NO_FULL_PAYOUT_INDICATION; else normal min profit. Then trailing/stop.",
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


def _entry_stop_floor_cents(entry_cents: int, fraction: float) -> int:
    return max(1, min(99, int(round(entry_cents * fraction))))


def _lock_floor_cents(
    settings: Settings, entry_ref_cents: int | None, peak_bid_cents: int | None
) -> int | None:
    """Minimum bid (¢) that locks at least ``TRADE_EXIT_LOCK_PROFIT_CENTS`` profit once peak has reached it."""
    lc = settings.trade_exit_lock_profit_cents
    if lc is None or lc <= 0 or entry_ref_cents is None or peak_bid_cents is None:
        return None
    if not (1 <= entry_ref_cents <= 99):
        return None
    need = float(entry_ref_cents) + float(lc)
    if float(peak_bid_cents) + 1e-9 < need:
        return None
    return max(1, min(99, int(round(entry_ref_cents + float(lc)))))


def _trailing_pullback_amount_cents(settings: Settings, peak_cents: int) -> float:
    c = float(settings.trade_exit_trailing_pullback_cents)
    p = float(settings.trade_exit_trailing_pullback_pct_of_peak)
    if p > 0:
        return max(c, float(peak_cents) * p)
    return c


def _is_loss_cutting_exit_reason(reason: str) -> bool:
    """Exit reasons that must not be blocked by hold-to-settlement (bid can disagree with mid)."""
    return reason in (
        "stop_loss_entry_fraction",
        "trailing_stop_pullback",
        "profit_lock_stop",
    )


def _exit_bypasses_hold_to_settlement(reason: str) -> bool:
    """Reasons that may exit even when implied chance suggests holding for settlement."""
    return _is_loss_cutting_exit_reason(reason) or reason == "take_profit_bid_vs_entry_multiplier"


def _exit_reason_matches_stop_rebuy(settings: Settings, reason: str) -> bool:
    if reason == "stop_loss_entry_fraction":
        return True
    if settings.trade_rebuy_after_stop_loss_include_trailing_and_profit_lock:
        return reason in ("trailing_stop_pullback", "profit_lock_stop")
    return False


def _lift_yes_ask_cents_from_ob(ob: Any) -> int | None:
    """Lift YES ask (¢) from complement of best NO bid; ``None`` if book unusable."""
    return lift_yes_ask_cents_from_orderbook(ob)


def _maybe_rebuy_yes_after_stop_loss(
    *,
    client: KalshiSdkClient,
    settings: Settings,
    risk: RiskManager,
    ledger: DryRunLedger | None,
    ticker: str,
    exit_reason: str,
    sold_count: int,
    log: StructuredLogger,
) -> None:
    """Optional: place a new buy YES on the same ticker after a stop-style exit (same gates as normal buys)."""
    if not settings.trade_rebuy_after_stop_loss_enabled:
        return
    if not _exit_reason_matches_stop_rebuy(settings, exit_reason):
        return

    delay = float(settings.trade_rebuy_after_stop_loss_delay_seconds)
    if delay > 0:
        time.sleep(delay)

    cd = float(settings.trade_rebuy_after_stop_loss_cooldown_seconds)
    if cd > 0:
        now = time.time()
        with _REBUY_COOLDOWN_LOCK:
            last = _REBUY_LAST_UNIX.get(ticker, 0.0)
            if now - last < cd:
                log.info(
                    "stop_loss_rebuy_skip",
                    ticker=ticker,
                    reason="cooldown",
                    cooldown_seconds=cd,
                    seconds_since_last=now - last,
                )
                return

    if should_skip_buy_ticker_substrings(settings, ticker):
        log.info("stop_loss_rebuy_skip", ticker=ticker, reason="ticker_substring_blocklist")
        return

    max_ask_cents = int(round(settings.trade_entry_effective_max_yes_ask_dollars * 100.0))
    max_ask_cents = max(1, min(99, max_ask_cents))

    ob = get_orderbook(client, ticker)
    lift = _lift_yes_ask_cents_from_ob(ob)
    if lift is None:
        log.info("stop_loss_rebuy_skip", ticker=ticker, reason="no_lift_yes_ask")
        return
    if lift > max_ask_cents:
        log.info(
            "stop_loss_rebuy_skip",
            ticker=ticker,
            reason="yes_ask_above_max",
            lift_yes_ask_cents=lift,
            max_yes_ask_cents=max_ask_cents,
        )
        return
    if skip_buy_yes_longshot(settings, lift):
        log.info(
            "stop_loss_rebuy_skip",
            ticker=ticker,
            reason="yes_ask_below_entry_min_floor",
            lift_yes_ask_cents=lift,
        )
        return

    limit = min(int(settings.strategy_limit_price_cents), int(lift))
    if limit < 1:
        log.info("stop_loss_rebuy_skip", ticker=ticker, reason="non_positive_limit", limit_yes_price_cents=limit)
        return

    rebuy_count = min(sold_count, settings.strategy_order_count, settings.max_contracts_per_market)
    rebuy_count = max(1, rebuy_count)
    tif = settings.trade_rebuy_after_stop_loss_time_in_force
    intent = make_limit_intent(
        ticker=ticker,
        side="yes",
        action="buy",
        count=rebuy_count,
        yes_price_cents=limit,
        time_in_force=tif,
    )
    _clear_peak_yes_bid(ticker)
    log.info(
        "stop_loss_rebuy_fire",
        ticker=ticker,
        count=rebuy_count,
        limit_yes_price_cents=limit,
        lift_yes_ask_cents=lift,
        prior_exit_reason=exit_reason,
        time_in_force=tif,
    )
    trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
    if cd > 0:
        with _REBUY_COOLDOWN_LOCK:
            _REBUY_LAST_UNIX[ticker] = time.time()


def _should_fire_exit(
    *,
    best_bid_cents: int,
    settings: Settings,
    cli_min_yes_bid_cents: int | None,
    entry_ref_cents: int | None,
    entry_source: Literal["manual", "portfolio", "none"] = "none",
    peak_bid_cents: int | None = None,
    implied_yes_chance_cents: int | None = None,
    min_profit_cents_effective: int | None = None,
) -> tuple[bool, str]:
    """True if we should submit a sell.

    **Priority:** optional hard TP when bid ≥ entry × ``TRADE_EXIT_TAKE_PROFIT_MIN_BID_VS_ENTRY_MULTIPLIER`` (>1)
    → take-profit (relaxed margin when no full-payout indication, else bid vs entry + min profit)
    → trailing / raised stop → classic stop-loss.

    Trailing: session peak best YES bid per ticker; when peak clears entry + activation, exit if
    bid ≤ combined stop (see ``TRADE_EXIT_TRAILING_BID_FRACTION_CAPS_PEAK_TRAIL``: min(peak−pullback, bid×fraction)
    or legacy max(entry×fraction, peak−pullback)). Raised fraction uses optional
    ``TRADE_EXIT_TRAILING_STOP_LOSS_FLOOR_FRACTION`` vs ``TRADE_EXIT_STOP_LOSS_ENTRY_FRACTION``.
    """
    skipped_suspect_stop = False
    would_have_stop_floor: int | None = None
    fixed_floor_base: int | None = None
    frac_base = float(settings.trade_exit_stop_loss_entry_fraction)

    if (
        settings.trade_exit_stop_loss_enabled
        and entry_ref_cents is not None
        and 1 <= entry_ref_cents <= 99
    ):
        skip_suspect = (
            settings.trade_exit_stop_loss_skip_suspect_portfolio_estimate
            and entry_source == "portfolio"
            and (entry_ref_cents >= 95 or entry_ref_cents <= 5)
        )
        if skip_suspect:
            skipped_suspect_stop = True
        else:
            fixed_floor_base = _entry_stop_floor_cents(entry_ref_cents, frac_base)
            would_have_stop_floor = fixed_floor_base

    armed = (
        settings.trade_exit_trailing_enabled
        and peak_bid_cents is not None
        and entry_ref_cents is not None
        and peak_bid_cents >= entry_ref_cents + settings.trade_exit_trailing_activate_above_entry_cents
    )
    frac_for_trailing = frac_base
    if armed and settings.trade_exit_trailing_stop_loss_floor_fraction is not None:
        frac_for_trailing = max(frac_for_trailing, float(settings.trade_exit_trailing_stop_loss_floor_fraction))

    t_min = settings.auto_sell_effective_min_yes_bid_cents(cli_min_yes_bid_cents)
    pct_hit = t_min is not None and best_bid_cents >= t_min

    mpc = settings.trade_exit_min_profit_cents_for_entry(entry_ref_cents)
    if min_profit_cents_effective is not None:
        mpc = min_profit_cents_effective
    profit_hit = False
    if mpc is not None and entry_ref_cents is not None:
        need_bid = int(math.ceil(float(entry_ref_cents) + mpc - 1e-9))
        profit_hit = best_bid_cents >= need_bid

    relaxed = settings.trade_exit_min_profit_cents_when_no_full_payout_indication
    ind = settings.trade_exit_full_payout_indication_min_chance_cents
    relaxed_hit = False
    if (
        relaxed > 0
        and ind > 0
        and implied_yes_chance_cents is not None
        and implied_yes_chance_cents < ind
        and entry_ref_cents is not None
        and 1 <= entry_ref_cents <= 99
    ):
        need_relaxed = int(math.ceil(float(entry_ref_cents) + float(relaxed) - 1e-9))
        relaxed_hit = best_bid_cents >= need_relaxed

    # 0) Hard take-profit vs entry multiple (+50% gain when multiplier=1.5: bid ≥ entry×1.5). Bypasses hold-to-settlement.
    mult = float(settings.trade_exit_take_profit_min_bid_vs_entry_multiplier)
    if mult > 1.0 and entry_ref_cents is not None and 1 <= entry_ref_cents <= 99:
        need_mult = int(math.ceil(float(entry_ref_cents) * mult - 1e-9))
        need_mult = max(1, min(99, need_mult))
        if best_bid_cents >= need_mult:
            return True, "take_profit_bid_vs_entry_multiplier"

    # 1) Take-profit (first): lower bar when book does not imply near-certain YES@$1
    if settings.trade_exit_only_profit_margin:
        if relaxed_hit:
            return True, "take_profit_profit_margin_no_full_payout_indication"
        if profit_hit:
            return True, "take_profit_profit_margin"
    else:
        if pct_hit and relaxed_hit:
            return True, "take_profit_implied_pct_and_margin_no_full_payout_indication"
        if pct_hit and profit_hit:
            return True, "take_profit_implied_pct_and_margin"
        if pct_hit:
            return True, "take_profit_implied_pct"
        if profit_hit:
            return True, "take_profit_profit_margin"

    lock_floor = _lock_floor_cents(settings, entry_ref_cents, peak_bid_cents)

    # 2) Trailing / combined stop + profit lock (peak ≥ entry+lock raises floor to entry+lock; then flux = trail)
    if (
        settings.trade_exit_trailing_enabled
        and armed
        and peak_bid_cents is not None
        and entry_ref_cents is not None
    ):
        pull = _trailing_pullback_amount_cents(settings, peak_bid_cents)
        trail_floor = max(1, min(99, int(math.ceil(float(peak_bid_cents) - pull - 1e-9))))
        if settings.trade_exit_trailing_combine_with_fixed_stop and not skipped_suspect_stop:
            if settings.trade_exit_trailing_bid_fraction_caps_peak_trail:
                # Fraction of **current** best bid moves down when price falls; cap peak−pullback so the
                # exit threshold does not stay stuck at the old high-water trail level only.
                bid_fraction_floor = _entry_stop_floor_cents(best_bid_cents, frac_for_trailing)
                eff_stop = min(trail_floor, bid_fraction_floor)
            else:
                fixed_for_max = _entry_stop_floor_cents(entry_ref_cents, frac_for_trailing)
                eff_stop = max(fixed_for_max, trail_floor)
        else:
            eff_stop = trail_floor
        if lock_floor is not None:
            eff_stop = max(eff_stop, lock_floor)
        if best_bid_cents <= eff_stop:
            return True, "trailing_stop_pullback"

    if lock_floor is not None and best_bid_cents <= lock_floor:
        return True, "profit_lock_stop"

    # 3) Classic fixed stop (entry × fraction; no trailing boost)
    if (
        settings.trade_exit_stop_loss_enabled
        and entry_ref_cents is not None
        and 1 <= entry_ref_cents <= 99
        and not skipped_suspect_stop
        and fixed_floor_base is not None
        and best_bid_cents <= fixed_floor_base
    ):
        return True, "stop_loss_entry_fraction"

    if settings.trade_exit_only_profit_margin:
        if (
            skipped_suspect_stop
            and would_have_stop_floor is not None
            and best_bid_cents <= would_have_stop_floor
        ):
            return False, "wait_stop_loss_skipped_suspect_portfolio_entry"
        return False, "wait_profit_only_mode"

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
    elif exit_reason.startswith("trailing_stop") or exit_reason.startswith("profit_lock"):
        kind = "trailing-stop " if exit_reason.startswith("trailing_stop") else "profit-lock "
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
        _clear_peak_yes_bid(ticker)
        if log_waits:
            log.info("auto_sell_skip", reason="no_long_yes", ticker=ticker, signed=signed)
        return "no_long_yes", None, None

    entry_ref = _resolve_entry_reference(settings, client, ticker, log)
    mult_tp = float(settings.trade_exit_take_profit_min_bid_vs_entry_multiplier)
    if entry_ref.cents is None and (
        settings.trade_exit_effective_min_profit_cents_per_contract is not None
        or settings.trade_exit_min_profit_pct_of_entry > 0
        or mult_tp > 1.0
    ):
        log.warning(
            "auto_sell_no_entry_reference",
            ticker=ticker,
            hint="set TRADE_EXIT_ENTRY_REFERENCE_YES_CENTS or enable TRADE_EXIT_ESTIMATE_ENTRY_FROM_PORTFOLIO "
            "(required for take-profit vs entry when using min profit, TRADE_EXIT_MIN_PROFIT_PCT_OF_ENTRY, "
            "or TRADE_EXIT_TAKE_PROFIT_MIN_BID_VS_ENTRY_MULTIPLIER>1)",
        )

    ob = get_orderbook(client, ticker)
    best = best_yes_bid_cents(ob)
    if best is None:
        if log_waits:
            log.info("auto_sell_skip", reason="no_yes_bids", ticker=ticker)
        return "no_yes_bids", None, None

    hmin = settings.trade_exit_hold_to_settlement_min_chance_cents
    sell_within = settings.trade_exit_sell_within_cents_of_max_payout
    rel = settings.trade_exit_min_profit_cents_when_no_full_payout_indication
    ind = settings.trade_exit_full_payout_indication_min_chance_cents
    chance: int | None = None
    if hmin > 0 or sell_within > 0 or (rel > 0 and ind > 0):
        chance = implied_yes_chance_cents_from_orderbook(ob, best)
    near_max = (
        sell_within > 0
        and chance is not None
        and chance >= (100 - sell_within)
    )
    peak = _update_peak_yes_bid(ticker, best)
    min_eff = _tape_relaxed_min_profit_cents_effective(
        client, settings, ticker, entry_ref.cents, log
    )
    fire, reason = _should_fire_exit(
        best_bid_cents=best,
        settings=settings,
        cli_min_yes_bid_cents=cli_min_yes_bid_cents,
        entry_ref_cents=entry_ref.cents,
        entry_source=entry_ref.source,
        peak_bid_cents=peak,
        implied_yes_chance_cents=chance,
        min_profit_cents_effective=min_eff,
    )
    if not fire and near_max:
        fire, reason = True, "take_profit_near_max_payout"
    in_hold = not near_max and hmin > 0 and chance is not None and chance >= hmin
    if in_hold and not (fire and _exit_bypasses_hold_to_settlement(reason)):
        if log_waits:
            log.info(
                "auto_sell_wait",
                ticker=ticker,
                reason="hold_to_settlement",
                implied_yes_chance_cents=chance,
                hold_min_chance_cents=hmin,
                best_yes_bid_cents=best,
            )
        return "wait", None, None

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

    # Exit the full long YES size (share count). Do not cap by MAX_CONTRACTS_PER_MARKET — that limits *entry*
    # batch size and order-size multiplier scaling, not how many shares we may flatten at one limit price.
    count = int(signed)
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
    if reason == "trailing_stop_pullback" and peak is not None:
        fire_extra["trailing_peak_yes_bid_cents"] = peak
        fire_extra["trailing_pullback_cents"] = settings.trade_exit_trailing_pullback_cents
    if reason == "profit_lock_stop" and entry_ref.cents is not None and settings.trade_exit_lock_profit_cents:
        fire_extra["profit_lock_floor_yes_bid_cents"] = max(
            1, min(99, int(round(entry_ref.cents + float(settings.trade_exit_lock_profit_cents))))
        )
    market_title = market_title_for_ticker(client, ticker)
    market_category = market_category_for_ticker(client, ticker)
    log.info(
        "auto_sell_fire",
        ticker=ticker,
        market_title=market_title,
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
        market_title=market_title,
        count=count,
        shares=count,
        limit_yes_price_cents=limit_cents,
        entry_yes_cents=entry_ref.cents,
        proceeds_cents=proceeds_cents,
        estimated_gross_profit_cents=gross_cents,
        exit_reason=reason,
        note="vs portfolio entry estimate; excludes fees; IOC may partially fill",
    )
    notify_auto_sell_outcome(
        settings,
        gross_profit_cents=gross_cents,
        exit_reason=reason,
        event_payload={
            "ticker": ticker,
            "market_title": market_title,
            "market_category": market_category,
            "count": count,
            "shares": count,
            "order_contracts": count,
            "limit_yes_price_cents": limit_cents,
            "entry_yes_cents": entry_ref.cents,
            "proceeds_cents": proceeds_cents,
            "estimated_gross_profit_cents": gross_cents,
            "exit_reason": reason,
            "note": "vs portfolio entry estimate; excludes fees; IOC may partially fill",
        },
    )
    _maybe_rebuy_yes_after_stop_loss(
        client=client,
        settings=settings,
        risk=risk,
        ledger=ledger,
        ticker=ticker,
        exit_reason=reason,
        sold_count=count,
        log=log,
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


def liquidate_all_long_yes_positions(
    client: KalshiSdkClient,
    settings: Settings,
    *,
    log: StructuredLogger,
    execute: bool,
) -> tuple[int, list[str]]:
    """Sell every long YES at best bid minus aggression (``TRADE_EXIT_SELL_*``); ignores take-profit rules.

    When ``execute`` is false, only prints intended orders. When true, submits via ``trade_execute`` (respects
    ``DRY_RUN`` / ``LIVE_TRADING``).
    """
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    snap = fetch_portfolio_snapshot(client, ticker=None)
    tickers = sorted(t for t, s in snap.positions_by_ticker.items() if s > 0)
    lines: list[str] = []
    n = 0
    for ticker in tickers:
        signed = float(snap.positions_by_ticker.get(ticker, 0.0))
        cnt = int(round(signed))
        if cnt < 1:
            lines.append(f"{ticker}: skip (position < 1 contract)")
            continue
        ob = get_orderbook(client, ticker)
        best = best_yes_bid_cents(ob)
        if best is None:
            lines.append(f"{ticker}: skip (no YES bids)")
            continue
        limit_cents = max(1, best - settings.trade_exit_sell_aggression_cents)
        tif = settings.trade_exit_sell_time_in_force
        intent = make_limit_intent(
            ticker=ticker,
            side="yes",
            action="sell",
            count=cnt,
            yes_price_cents=limit_cents,
            time_in_force=tif,
        )
        title = (market_title_for_ticker(client, ticker) or "")[:80]
        if not execute:
            lines.append(f"{ticker}: would sell {cnt} YES @ {limit_cents}¢ — {title}")
            continue
        log.info(
            "liquidate_all_long_yes",
            ticker=ticker,
            market_title=title,
            count=cnt,
            limit_yes_price_cents=limit_cents,
            best_yes_bid_cents=best,
            time_in_force=tif,
        )
        trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
        n += 1
        lines.append(f"{ticker}: submitted sell {cnt} YES @ {limit_cents}¢")
        sp = settings.trade_submit_spacing_seconds
        if sp > 0:
            time.sleep(float(sp))
    return n, lines


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
