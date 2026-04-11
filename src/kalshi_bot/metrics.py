"""Performance and robustness metrics for research (not profitability guarantees).

All statistics are descriptive; past simulation results do not predict future returns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

# --- Disclaimer: embedded in reports ---
NO_GUARANTEE_DISCLAIMER = (
    "These metrics are descriptive summaries of simulated or historical samples. "
    "They do not guarantee future profitability or real-world fill quality."
)


@dataclass(frozen=True)
class TradeOutcome:
    """Single closed trade for metric aggregation."""

    pnl_cents: float
    edge_estimate_cents: float  # e.g. mid at entry vs exit mid proxy


def max_drawdown(equity_cents: Sequence[float]) -> float:
    """Max peak-to-trough decline as a fraction of peak (non-positive)."""
    if not equity_cents:
        return 0.0
    peak = float(equity_cents[0])
    max_dd = 0.0
    for x in equity_cents:
        if x > peak:
            peak = x
        dd = (x - peak) / peak if peak != 0 else 0.0
        if dd < max_dd:
            max_dd = dd
    return max_dd


def sharpe_like(
    returns: Sequence[float],
    *,
    periods_per_year: float = 252.0,
    eps: float = 1e-12,
) -> float:
    """Mean/std of per-period returns, annualized by sqrt scaling (Sharpe-like, not a claim of normality)."""
    if len(returns) < 2:
        return 0.0
    m = sum(returns) / len(returns)
    var = sum((r - m) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var) if var > 0 else eps
    return (m / std) * math.sqrt(periods_per_year)


def win_rate(trades: Iterable[TradeOutcome]) -> float:
    wins = [t for t in trades if t.pnl_cents > 0]
    ts = list(trades)
    if not ts:
        return 0.0
    return len(wins) / len(ts)


def average_edge_estimate(trades: Iterable[TradeOutcome]) -> float:
    ts = list(trades)
    if not ts:
        return 0.0
    return sum(t.edge_estimate_cents for t in ts) / len(ts)


@dataclass
class SensitivityResult:
    """One grid point for fee/slippage stress."""

    fee_cents_per_contract: float
    slippage_cents_per_contract: float
    net_pnl_cents: float
    max_drawdown: float


def fee_slippage_sensitivity(
    *,
    base_trades: Sequence[TradeOutcome],
    equity_curve: Sequence[float],
    fee_grid: Sequence[float],
    slippage_grid: Sequence[float],
    contracts_per_trade: Sequence[int] | None = None,
) -> list[SensitivityResult]:
    """Recompute approximate net PnL under alternate fee and slippage per contract.

    Assumes each trade pays `fee + slippage` per contract in cents (linear stress test).
    """
    n = len(base_trades)
    if contracts_per_trade is None:
        contracts_per_trade = [1] * n
    out: list[SensitivityResult] = []
    base_pnl = sum(t.pnl_cents for t in base_trades)
    for fee in fee_grid:
        for slip in slippage_grid:
            drag = sum((fee + slip) * c for c in contracts_per_trade[:n])
            net = base_pnl - drag
            # Approximate equity as scalar shift (DD unchanged in structure) — report base DD
            dd = max_drawdown(equity_curve) if equity_curve else 0.0
            out.append(
                SensitivityResult(
                    fee_cents_per_contract=fee,
                    slippage_cents_per_contract=slip,
                    net_pnl_cents=net,
                    max_drawdown=dd,
                )
            )
    return out


def format_report(
    *,
    trades: Sequence[TradeOutcome],
    equity_cents: Sequence[float],
    periods_per_year: float = 252.0,
) -> str:
    """Human-readable summary for CLI / logs."""
    rets: list[float] = []
    for i in range(1, len(equity_cents)):
        a, b = equity_cents[i - 1], equity_cents[i]
        if a != 0:
            rets.append((b - a) / abs(a))

    lines = [
        NO_GUARANTEE_DISCLAIMER,
        f"Trades: {len(trades)}",
        f"Win rate: {win_rate(trades):.2%}",
        f"Avg edge estimate (cents): {average_edge_estimate(trades):.4f}",
        f"Max drawdown (fraction): {max_drawdown(equity_cents):.4f}",
        f"Sharpe-like (returns-based): {sharpe_like(rets, periods_per_year=periods_per_year):.4f}",
    ]
    return "\n".join(lines)


def walk_forward_indices(n: int, n_windows: int, train_ratio: float) -> Iterator[tuple[range, range]]:
    """Yield (train_index_range, test_index_range) for walk-forward evaluation."""
    if n_windows < 1 or n < 2:
        return
    window = max(1, n // n_windows)
    for w in range(n_windows):
        start = w * window
        end = min(n, start + window)
        if end - start < 2:
            continue
        split = start + int((end - start) * train_ratio)
        train = range(start, split)
        test = range(split, end)
        if train.stop <= train.start or test.stop <= test.start:
            continue
        yield train, test
