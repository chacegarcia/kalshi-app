"""Fee-aware expectancy from structured JSONL (auto-sell closes).

Parses ``auto_sell_profit_estimate`` events and estimates net P/L after Kalshi taker fees
on entry + exit. Outputs break-even average win and suggested min-profit heuristics — not
investment advice; small samples are noisy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from kalshi_bot.config import project_root
from kalshi_bot.fees import kalshi_general_taker_fee_usd


def _round_trip_taker_fee_cents(*, entry_cents: int, exit_cents: int, contracts: int) -> float:
    """Both legs as taker (conservative vs maker exits)."""
    c = max(1, int(contracts))
    ein = kalshi_general_taker_fee_usd(contracts=c, price_dollars=entry_cents / 100.0)
    ex = kalshi_general_taker_fee_usd(contracts=c, price_dollars=exit_cents / 100.0)
    return (ein + ex) * 100.0


def _mean(xs: list[float]) -> float | None:
    if not xs:
        return None
    return sum(xs) / len(xs)


@dataclass(frozen=True)
class ClosedExit:
    """One auto-sell close from logs."""

    ticker: str
    exit_reason: str
    contracts: int
    entry_cents: int | None
    exit_limit_cents: int
    gross_profit_cents: int | None
    fee_cents: float | None
    net_profit_cents: float | None


def _payload_exit_reason(payload: dict[str, Any], fallback: str | None) -> str:
    r = payload.get("exit_reason")
    if isinstance(r, str) and r:
        return r
    t = payload.get("trigger")
    if isinstance(t, str) and t:
        return t
    return fallback or "unknown"


def iter_closed_exits_from_jsonl(
    path: Path,
    *,
    max_lines: int = 200_000,
    max_bytes: int = 80_000_000,
) -> Iterator[ClosedExit]:
    """Yield closes from JSONL; reads tail only for large files.

    Older logs without ``exit_reason`` on ``auto_sell_profit_estimate`` are matched to the
    most recent ``auto_sell_fire`` for the same ticker (best-effort).
    """
    if not path.exists():
        return
    try:
        raw = path.read_bytes()
    except OSError:
        return
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    lines = lines[-max_lines:]

    last_fire_reason: dict[str, str] = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        event = obj.get("event")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue

        if event == "auto_sell_fire":
            t = payload.get("ticker")
            tr = payload.get("trigger")
            if isinstance(t, str) and isinstance(tr, str) and tr:
                last_fire_reason[t] = tr
            continue

        if event != "auto_sell_profit_estimate":
            continue

        ticker = payload.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            continue

        fb = last_fire_reason.get(ticker)
        exit_reason = _payload_exit_reason(payload, fb)

        count = payload.get("shares", payload.get("count", 1))
        try:
            contracts = max(1, int(count))
        except (TypeError, ValueError):
            contracts = 1

        lim = payload.get("limit_yes_price_cents")
        try:
            exit_limit = int(lim) if lim is not None else 0
        except (TypeError, ValueError):
            exit_limit = 0
        if not (1 <= exit_limit <= 99):
            continue

        ent = payload.get("entry_yes_cents")
        entry_cents: int | None = None
        if ent is not None:
            try:
                e = int(ent)
                if 1 <= e <= 99:
                    entry_cents = e
            except (TypeError, ValueError):
                pass

        gp = payload.get("estimated_gross_profit_cents")
        gross: int | None = None
        if gp is not None:
            try:
                gross = int(gp)
            except (TypeError, ValueError):
                gross = None

        fee: float | None = None
        net: float | None = None
        if entry_cents is not None and gross is not None:
            fee = _round_trip_taker_fee_cents(
                entry_cents=entry_cents,
                exit_cents=exit_limit,
                contracts=contracts,
            )
            net = float(gross) - fee

        yield ClosedExit(
            ticker=ticker,
            exit_reason=exit_reason,
            contracts=contracts,
            entry_cents=entry_cents,
            exit_limit_cents=exit_limit,
            gross_profit_cents=gross,
            fee_cents=fee,
            net_profit_cents=net,
        )


@dataclass(frozen=True)
class ExpectancyStats:
    n: int
    n_net_known: int
    n_wins: int
    n_losses: int
    n_breakeven: int
    win_rate: float | None
    avg_net_win_cents: float | None
    avg_net_loss_magnitude_cents: float | None
    avg_net_per_trade_cents: float | None
    break_even_avg_win_cents: float | None
    fee_round_trip_avg_cents: float | None


def compute_expectancy_stats(closes: list[ClosedExit]) -> ExpectancyStats:
    nets: list[float] = []
    wins: list[float] = []
    losses: list[float] = []
    fees: list[float] = []

    for c in closes:
        if c.net_profit_cents is None:
            continue
        nets.append(c.net_profit_cents)
        if c.fee_cents is not None:
            fees.append(c.fee_cents)
        if c.net_profit_cents > 0:
            wins.append(c.net_profit_cents)
        elif c.net_profit_cents < 0:
            losses.append(-c.net_profit_cents)
        # else breakeven

    n = len(closes)
    n_net = len(nets)
    n_wins = len(wins)
    n_losses = len(losses)
    n_be = n_net - n_wins - n_losses

    wr = n_wins / n_net if n_net else None
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    avg_net = _mean(nets)
    avg_fee = _mean(fees) if fees else None

    be: float | None = None
    if wr is not None and avg_loss is not None and wr > 1e-9 and wr < 1.0 - 1e-9:
        # E = p * W - (1-p) * L  =>  break-even when p * W = (1-p) * L  =>  W = (1-p)/p * L
        be = (1.0 - wr) / wr * avg_loss
    elif wr is not None and n_losses == 0 and n_wins > 0:
        be = 0.0
    elif wr is not None and n_wins == 0 and n_losses > 0:
        be = None

    return ExpectancyStats(
        n=n,
        n_net_known=n_net,
        n_wins=n_wins,
        n_losses=n_losses,
        n_breakeven=n_be,
        win_rate=wr,
        avg_net_win_cents=avg_win,
        avg_net_loss_magnitude_cents=avg_loss,
        avg_net_per_trade_cents=avg_net,
        break_even_avg_win_cents=be,
        fee_round_trip_avg_cents=avg_fee,
    )


def format_expectancy_report(stats: ExpectancyStats, *, log_path: Path) -> str:
    lines = [
        "--- exit expectancy (from structured log) ---",
        f"  Log: {log_path}",
        f"  Closes parsed: {stats.n}  (with net estimate after taker fees: {stats.n_net_known})",
    ]
    if stats.n_net_known == 0:
        lines.append(
            "  No closes with both entry and gross P/L — need auto_sell_profit_estimate rows with entry + estimated_gross_profit_cents."
        )
        lines.append("---")
        return "\n".join(lines)

    lines.append(
        f"  Wins / losses / ~flat: {stats.n_wins} / {stats.n_losses} / {stats.n_breakeven}  "
        f"(win rate: {100.0 * stats.win_rate:.1f}%)" if stats.win_rate is not None else "  Win rate: n/a"
    )
    if stats.avg_net_per_trade_cents is not None:
        lines.append(f"  Avg net ¢/close (after fee model): {stats.avg_net_per_trade_cents:.2f}¢")
    if stats.avg_net_win_cents is not None:
        lines.append(f"  Avg net ¢ on winning closes: {stats.avg_net_win_cents:.2f}¢")
    if stats.avg_net_loss_magnitude_cents is not None:
        lines.append(f"  Avg |net ¢| on losing closes: {stats.avg_net_loss_magnitude_cents:.2f}¢")
    if stats.fee_round_trip_avg_cents is not None:
        lines.append(f"  Avg modeled round-trip taker fee: {stats.fee_round_trip_avg_cents:.2f}¢")

    lines.append("  ---")
    if stats.break_even_avg_win_cents is not None and stats.win_rate is not None:
        p = stats.win_rate
        lines.append(
            f"  Formula (net cents, after modeled taker fees on buy+sell): "
            f"W_break_even = ((1-p)/p) * L  with p={p:.4f}, "
            f"L = avg |net loss|."
        )
        lines.append(
            f"  Break-even: average **net** win (after fees) must be ≥ "
            f"{stats.break_even_avg_win_cents:.2f}¢ "
            f"to match this win rate and avg loss size (ignores selection drift; not advice)."
        )
        if stats.avg_net_win_cents is not None:
            gap = stats.break_even_avg_win_cents - stats.avg_net_win_cents
            lines.append(
                f"  vs your avg net win: {stats.avg_net_win_cents:.2f}¢  →  "
                f"{'shortfall' if gap > 0.5 else 'surplus' if gap < -0.5 else 'about on pace'} "
                f"≈ {gap:+.2f}¢ vs break-even target."
            )
    elif stats.win_rate is not None and stats.win_rate >= 1.0 - 1e-9:
        lines.append("  Break-even target: n/a (no losing closes in sample — sample may be too short).")
    elif stats.win_rate is not None and stats.win_rate <= 1e-9:
        lines.append("  Break-even target: n/a (no winning closes — fix entry edge or risk before tuning exits).")

    lines.append(
        "  Hint: raise TRADE_EXIT_MIN_PROFIT_* / lower noise exits so **average** net wins clear the break-even line;"
        " tighten entries (TRADE_MIN_NET_EDGE_AFTER_FEES); fees dominate sub–few-¢ gross targets."
    )
    if stats.n_net_known < 30:
        lines.append(
            f"  Note: only {stats.n_net_known} closes with fee-adjusted net — variance is high; revisit after more trades."
        )
    lines.append("---")
    return "\n".join(lines)


def default_structured_log_path() -> Path:
    raw = os.environ.get("STRUCTURED_LOG_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return project_root() / "logs" / "kalshi_bot.jsonl"


def run_expectancy_report(*, log_path: Path | None, max_lines: int) -> str:
    path = log_path or default_structured_log_path()
    closes = list(iter_closed_exits_from_jsonl(path, max_lines=max_lines))
    stats = compute_expectancy_stats(closes)
    return format_expectancy_report(stats, log_path=path)
