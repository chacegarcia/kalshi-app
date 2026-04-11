"""Scan open Kalshi markets for boxed YES+NO surplus and fee-adjusted directional edges."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings
from kalshi_bot.edge_math import (
    boxed_arb_surplus_after_taker_fees_dollars,
    boxed_arb_surplus_before_fees_dollars,
    implied_yes_ask_dollars,
    net_edge_buy_yes_long,
)
from kalshi_bot.market_data import (
    best_no_bid_cents,
    best_yes_bid_cents,
    get_orderbook,
    list_open_markets,
    summarize_market_row,
)


@dataclass
class ScanRow:
    ticker: str
    title: str
    yes_bid_c: int | None
    no_bid_c: int | None
    boxed_surplus_before_fees_dollars: float | None
    boxed_surplus_after_fees_dollars: float | None
    edge_buy_yes: float | None


def _dollars_from_cents(c: int | None) -> float | None:
    if c is None:
        return None
    return c / 100.0


def scan_kalshi_opportunities(
    client: KalshiSdkClient,
    settings: Settings,
    *,
    limit: int = 40,
    use_llm_fair: bool = False,
) -> list[ScanRow]:
    """Fetch open markets + orderbooks; rank boxed arb + optional fair-value edge."""
    from kalshi_bot.llm_screen import optional_llm_fair_yes

    resp = list_open_markets(client, limit=limit)
    markets = list(getattr(resp, "markets", []) or [])
    out: list[ScanRow] = []

    for m in markets:
        s = summarize_market_row(m)
        ticker = s.ticker
        try:
            ob = get_orderbook(client, ticker)
        except Exception:
            continue

        yb = best_yes_bid_cents(ob)
        nb = best_no_bid_cents(ob)
        ybd = _dollars_from_cents(yb)
        nbd = _dollars_from_cents(nb)

        boxed_before = None
        boxed_after = None
        if ybd is not None and nbd is not None:
            boxed_before = boxed_arb_surplus_before_fees_dollars(ybd, nbd)
            boxed_after = boxed_arb_surplus_after_taker_fees_dollars(ybd, nbd, contracts=1)

        fair = settings.trade_fair_yes_prob
        if use_llm_fair:
            fair = optional_llm_fair_yes(s.title, ticker=ticker, settings=settings) or fair

        edge_buy = None
        if fair is not None and ybd is not None and nbd is not None:
            ya = implied_yes_ask_dollars(nbd)
            edge_buy = net_edge_buy_yes_long(fair_yes=fair, yes_ask_dollars=ya, contracts=settings.strategy_order_count)

        out.append(
            ScanRow(
                ticker=ticker,
                title=s.title[:120],
                yes_bid_c=yb,
                no_bid_c=nb,
                boxed_surplus_before_fees_dollars=boxed_before,
                boxed_surplus_after_fees_dollars=boxed_after,
                edge_buy_yes=edge_buy,
            )
        )

    out.sort(
        key=lambda r: (
            -(r.boxed_surplus_after_fees_dollars or -1e9),
            -(r.edge_buy_yes or -1e9),
        )
    )
    return out


def format_scan_report(rows: list[ScanRow], *, min_boxed_after: float = -1.0, min_edge: float = -1.0) -> str:
    lines = [
        "ticker\tboxed$_after_fees\tedge_vs_fair\tY_bid\tN_bid\ttitle",
        "—" * 100,
    ]
    use_filter = min_boxed_after >= 0.0 or min_edge >= 0.0
    for r in rows:
        if use_filter:
            if (r.boxed_surplus_after_fees_dollars or 0) < min_boxed_after and (r.edge_buy_yes or 0) < min_edge:
                continue
        lines.append(
            f"{r.ticker}\t{r.boxed_surplus_after_fees_dollars or 0:.4f}\t{r.edge_buy_yes or 0:.4f}\t{r.yes_bid_c}\t{r.no_bid_c}\t{r.title[:60]}"
        )
    return "\n".join(lines)
