"""Backtesting, walk-forward splits, out-of-sample evaluation, and parameter sweeps.

Feed historical **snapshots** or **recorded JSONL** from your own data pipeline.
This module does not download Kalshi history for you — wire your own loaders.

**Plug-in point:** `strategy_signal_fn` — replace with your rule engine taking
`PriceRecord` and returning `TradeIntent | None`.
"""

from __future__ import annotations

import json
import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_bot.metrics import (
    format_report,
    max_drawdown,
    sharpe_like,
    walk_forward_indices,
    win_rate,
)
from kalshi_bot.paper_engine import MarketSnapshot, PaperFillConfig, PaperPortfolio, simulate_fill
from kalshi_bot.strategy import TradeIntent


@dataclass
class PriceRecord:
    """One row of time series for research (extend with fields you record)."""

    ts: float
    ticker: str
    yes_bid_dollars: float
    yes_ask_dollars: float


def load_price_records_jsonl(path: Path) -> list[PriceRecord]:
    """Load JSONL with keys: ts, ticker, yes_bid_dollars, yes_ask_dollars."""
    rows: list[PriceRecord] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rows.append(
                PriceRecord(
                    ts=float(d["ts"]),
                    ticker=str(d["ticker"]),
                    yes_bid_dollars=float(d["yes_bid_dollars"]),
                    yes_ask_dollars=float(d["yes_ask_dollars"]),
                )
            )
    rows.sort(key=lambda r: r.ts)
    return rows


def run_rule_backtest(
    records: Sequence[PriceRecord],
    *,
    strategy_signal_fn: Callable[[PriceRecord], TradeIntent | None],
    paper_cfg: PaperFillConfig,
    initial_cash_cents: float = 100_000.0,
    rng: Any | None = None,
) -> tuple[list[TradeOutcome], list[float], PaperPortfolio]:
    """Walk records in order; apply strategy; simulate fills; return trades + equity curve."""
    import random

    rng = rng or random.Random(42)
    port = PaperPortfolio(cash_cents=initial_cash_cents)
    trades: list[TradeOutcome] = []

    for rec in records:
        snap = MarketSnapshot(yes_bid_dollars=rec.yes_bid_dollars, yes_ask_dollars=rec.yes_ask_dollars)
        mid_c = snap.mid_dollars * 100.0
        intent = strategy_signal_fn(rec)
        if intent is None:
            port.mark_equity(mid_c)
            continue
        out, fee = simulate_fill(intent, snap, paper_cfg, rng)
        if out is not None:
            filled = intent.count * paper_cfg.partial_fill_fraction
            price = float(intent.yes_price_cents)
            port.apply_buy_yes(contracts=filled, price_cents=price, fee_slippage_cents=fee)
            trades.append(out)
        port.mark_equity(mid_c)

    equity = port.equity_history
    return trades, equity, port


def walk_forward_eval(
    records: Sequence[PriceRecord],
    *,
    n_windows: int,
    train_ratio: float,
    strategy_factory: Callable[[dict[str, Any]], Callable[[PriceRecord], TradeIntent | None]],
    param: dict[str, Any],
    paper_cfg: PaperFillConfig,
) -> list[dict[str, Any]]:
    """Train window is currently a stub (params fixed); test reports OOS metrics per window.

    **Plug-in point:** replace `strategy_factory` to fit parameters on `train` records only.
    """
    n = len(records)
    results: list[dict[str, Any]] = []
    strat_fn = strategy_factory(param)
    for train_idx, test_idx in walk_forward_indices(n, n_windows, train_ratio):
        train = [records[i] for i in train_idx]
        test = [records[i] for i in test_idx]
        _ = train  # reserved for future calibration
        tr, eq, _ = run_rule_backtest(test, strategy_signal_fn=strat_fn, paper_cfg=paper_cfg)
        rets = []
        for i in range(1, len(eq)):
            a, b = eq[i - 1], eq[i]
            if a != 0:
                rets.append((b - a) / abs(a))
        results.append(
            {
                "train_len": len(train),
                "test_len": len(test),
                "oos_win_rate": win_rate(tr),
                "oos_max_dd": max_drawdown(eq) if eq else 0.0,
                "oos_sharpe_like": sharpe_like(rets, periods_per_year=252.0) if len(rets) > 1 else 0.0,
            }
        )
    return results


def parameter_sweep(
    records: Sequence[PriceRecord],
    *,
    grid: dict[str, list[Any]],
    strategy_factory: Callable[[dict[str, Any]], Callable[[PriceRecord], TradeIntent | None]],
    paper_cfg: PaperFillConfig,
) -> list[dict[str, Any]]:
    """Exhaustive Cartesian sweep over `grid` keys (small grids only)."""
    keys = list(grid.keys())
    out: list[dict[str, Any]] = []
    value_lists = [grid[k] for k in keys]
    for combo in itertools.product(*value_lists):
        param = dict(zip(keys, combo, strict=True))
        tr, eq, _ = run_rule_backtest(
            records,
            strategy_signal_fn=strategy_factory(param),
            paper_cfg=paper_cfg,
        )
        out.append(
            {
                "params": param,
                "n_trades": len(tr),
                "max_dd": max_drawdown(eq) if eq else 0.0,
                "report": format_report(trades=tr, equity_cents=eq),
            }
        )
    return out
