"""Watch open long-YES positions: book, recent tape lean, short-horizon candle drift.

Uses per-ticker ``get_trades(ticker=...)`` (not the global tape). Educational / situational awareness only.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings
from kalshi_bot.edge_math import implied_yes_ask_dollars
from kalshi_bot.logger import StructuredLogger, get_logger
from kalshi_bot.market_data import (
    TakerTapeLean,
    fetch_public_trades_for_ticker,
    fetch_yes_close_prices,
    get_orderbook,
    market_title_for_ticker,
    summarize_taker_tape_lean,
    yes_bid_and_no_bid_cents_for_trading,
)
from kalshi_bot.portfolio import (
    estimate_yes_entry_cents_from_position,
    fetch_portfolio_snapshot,
    get_market_position_row,
)


@dataclass
class PositionWatchRow:
    ticker: str
    title: str
    long_yes_shares: float
    best_yes_bid_cents: int | None
    implied_yes_mid_cents: int | None
    spread_cents: int | None
    entry_yes_cents: int | None
    unrealized_pnl_cents_per_share: float | None
    tape: TakerTapeLean
    candle_net_change_dollars: float | None
    candle_bars: int
    detail: str


def _implied_mid_and_spread_cents(ob: object) -> tuple[int | None, int | None, int | None]:
    yb, nb = yes_bid_and_no_bid_cents_for_trading(ob)
    if yb is None:
        return None, None, None
    if nb is None:
        return int(yb), int(yb), None
    ya = implied_yes_ask_dollars(nb / 100.0)
    ya_c = int(max(1, min(99, round(ya * 100.0))))
    mid = int(round((float(yb) + float(ya_c)) / 2.0))
    mid = max(1, min(99, mid))
    sp = max(0, ya_c - int(yb))
    return int(yb), mid, sp


def collect_position_watch_rows(
    client: KalshiSdkClient,
    settings: Settings,
    *,
    max_trades_per_ticker: int = 200,
    include_candles: bool = True,
    candle_period_minutes: int = 5,
    candle_lookback_minutes: int = 120,
    log: StructuredLogger | None = None,
) -> list[PositionWatchRow]:
    """One row per long-YES position: book + tape lean + optional candle move."""
    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    snap = fetch_portfolio_snapshot(client, ticker=None)
    tickers = sorted(t for t, s in snap.positions_by_ticker.items() if float(s) > 0)
    rows: list[PositionWatchRow] = []

    for ticker in tickers:
        shares = float(snap.positions_by_ticker.get(ticker, 0.0))
        title = (market_title_for_ticker(client, ticker) or "").strip() or ticker
        entry_c: int | None = None
        row_raw = get_market_position_row(client, ticker)
        if row_raw is not None:
            entry_c = estimate_yes_entry_cents_from_position(row_raw)

        ob = get_orderbook(client, ticker)
        bid_c, mid_c, sp_c = _implied_mid_and_spread_cents(ob)

        unreal: float | None = None
        if entry_c is not None and mid_c is not None:
            unreal = float(mid_c) - float(entry_c)

        trades = fetch_public_trades_for_ticker(client, ticker, max_trades=max_trades_per_ticker)
        lean = summarize_taker_tape_lean(trades)

        c_net: float | None = None
        c_n = 0
        detail = ""
        if include_candles:
            try:
                closes = fetch_yes_close_prices(
                    client,
                    ticker,
                    period_interval_minutes=candle_period_minutes,
                    lookback_seconds=max(300, candle_lookback_minutes * 60),
                )
                c_n = len(closes)
                if len(closes) >= 2:
                    c_net = closes[-1] - closes[0]
            except Exception as exc:  # noqa: BLE001
                detail = f"candles: {exc!s}"

        rows.append(
            PositionWatchRow(
                ticker=ticker,
                title=title,
                long_yes_shares=shares,
                best_yes_bid_cents=bid_c,
                implied_yes_mid_cents=mid_c,
                spread_cents=sp_c,
                entry_yes_cents=entry_c,
                unrealized_pnl_cents_per_share=unreal,
                tape=lean,
                candle_net_change_dollars=c_net,
                candle_bars=c_n,
                detail=detail,
            )
        )
        log.debug(
            "position_watch_row",
            ticker=ticker,
            lean=lean.lean_label(),
            yes_share=lean.yes_share,
            tape_trades=lean.trade_count,
            mid_cents=mid_c,
        )

    return rows


def format_position_watch_lines(rows: list[PositionWatchRow]) -> list[str]:
    """Human-readable table lines."""
    out: list[str] = [
        "--- positions-watch (long YES) — tape = taker-side lean on this ticker only ---",
        f"  {'ticker':<28} {'sh':>5} {'bid':>4} {'mid':>4} {'spr':>3} {'entry':>5} {'uPnL':>6}  {'tape':<10} {'Y%':>4}  {'Δchart':>8}",
    ]
    for r in rows:
        bid = f"{r.best_yes_bid_cents}" if r.best_yes_bid_cents is not None else "—"
        mid = f"{r.implied_yes_mid_cents}" if r.implied_yes_mid_cents is not None else "—"
        spr = f"{r.spread_cents}" if r.spread_cents is not None else "—"
        ent = f"{r.entry_yes_cents}" if r.entry_yes_cents is not None else "—"
        upnl = f"{r.unrealized_pnl_cents_per_share:+.1f}" if r.unrealized_pnl_cents_per_share is not None else "—"
        ypct = "—"
        if r.tape.yes_share is not None:
            ypct = f"{100.0 * r.tape.yes_share:.0f}"
        dch = "—"
        if r.candle_net_change_dollars is not None:
            dch = f"{r.candle_net_change_dollars * 100.0:+.1f}¢"
        out.append(
            f"  {r.ticker:<28} {r.long_yes_shares:5.1f} {bid:>4} {mid:>4} {spr:>3} {ent:>5} {upnl:>6}  "
            f"{r.tape.lean_label():<10} {ypct:>4}  {dch:>8}"
        )
        if r.detail:
            out.append(f"    note: {r.detail}")
        if r.title.strip():
            out.append(f"    {r.title}")
    if not rows:
        out.append("  (no long YES positions)")
    out.append("---")
    return out


def rows_to_json(rows: list[PositionWatchRow]) -> str:
    """JSON for scripting (tape as dict)."""
    payload = []
    for r in rows:
        d = asdict(r)
        d["tape"] = asdict(r.tape)
        payload.append(d)
    return json.dumps(payload, indent=2)


def run_position_watch_loop(
    client: KalshiSdkClient,
    settings: Settings,
    *,
    interval_seconds: float,
    json_mode: bool,
    include_candles: bool,
    max_trades_per_ticker: int,
) -> None:
    """Print watch snapshot; repeat until Ctrl+C."""
    while True:
        rows = collect_position_watch_rows(
            client,
            settings,
            max_trades_per_ticker=max_trades_per_ticker,
            include_candles=include_candles,
        )
        if json_mode:
            print(rows_to_json(rows), flush=True)
        else:
            for line in format_position_watch_lines(rows):
                print(line, flush=True)
        print(f"(next refresh in {interval_seconds:.0f}s — Ctrl+C to stop)\n", flush=True)
        time.sleep(max(5.0, interval_seconds))
