"""LLM-assisted market scan: model reasons about each market; we enforce bot math then optionally trade."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.config import Settings
from kalshi_bot.edge_math import implied_yes_ask_dollars, min_edge_threshold_for_mid, net_edge_buy_yes_long
from kalshi_bot.execution import DryRunLedger
from kalshi_bot.logger import StructuredLogger, get_logger
from kalshi_bot.market_data import best_no_bid_cents, best_yes_bid_cents, get_orderbook, list_open_markets, summarize_market_row
from kalshi_bot.portfolio import get_balance_cents
from kalshi_bot.risk import RiskManager
from kalshi_bot.sizing import effective_max_contracts
from kalshi_bot.trading import build_sdk_client, make_limit_intent, trade_execute
from kalshi_bot.llm_screen import LLMOpportunityVerdict, llm_evaluate_opportunity


@dataclass
class LLMTradeRunStats:
    """Counts why markets did not become orders (see end-of-run summary)."""

    markets: int = 0
    skip_orderbook: int = 0
    skip_no_bids: int = 0
    llm_no_verdict: int = 0
    llm_declined: int = 0
    bot_math_rejected: int = 0
    skip_low_volume: int = 0
    skipped_cli_no_execute: int = 0
    blocked_trade_llm_auto_execute_false: int = 0
    submitted: int = 0

    def lines(self) -> list[str]:
        return [
            "--- llm-trade summary (why no orders?) ---",
            f"  markets scanned:              {self.markets}",
            f"  skip (orderbook error):       {self.skip_orderbook}",
            f"  skip (no YES/NO bids):        {self.skip_no_bids}",
            f"  LLM no JSON / API fail:       {self.llm_no_verdict}",
            f"  LLM declined (approve/buy):   {self.llm_declined}  <-- usually #1 when this is high",
            f"  blocked by bot math (edge):   {self.bot_math_rejected}",
            f"  skip (volume below min):      {self.skip_low_volume}",
            f"  skipped (--execute false):    {self.skipped_cli_no_execute}",
            f"  TRADE_LLM_AUTO_EXECUTE false: {self.blocked_trade_llm_auto_execute_false}",
            f"  reached trade_execute:        {self.submitted}",
            "  (If this > 0 but you see no live orders: check logs for order_blocked / refused / dry_run.)",
            "---",
        ]


def _passes_bot_edge(
    settings: Settings,
    *,
    fair_yes: float,
    yes_bid_dollars: float,
    yes_ask_dollars: float,
    contracts: int,
) -> bool:
    """Deterministic gate: LLM cannot bypass fee-aware edge + mid penalty."""
    edge = net_edge_buy_yes_long(fair_yes=fair_yes, yes_ask_dollars=yes_ask_dollars, contracts=contracts)
    mid = (yes_bid_dollars + yes_ask_dollars) / 2.0
    need = min_edge_threshold_for_mid(
        mid,
        base_min_edge=settings.trade_min_net_edge_after_fees,
        middle_extra=settings.trade_edge_middle_extra_edge,
    )
    if edge < need:
        return False
    if yes_ask_dollars > settings.strategy_max_yes_ask_dollars:
        return False
    spread = max(0.0, yes_ask_dollars - yes_bid_dollars)
    if spread < settings.strategy_min_spread_dollars:
        return False
    if settings.trade_max_entry_spread_dollars is not None and spread > settings.trade_max_entry_spread_dollars:
        return False
    return True


def run_llm_opportunity_pipeline(
    settings: Settings,
    *,
    execute: bool,
    log: StructuredLogger | None = None,
) -> tuple[int, LLMTradeRunStats]:
    """Scan open markets, LLM verdict + bot math; optionally ``trade_execute`` when ``execute`` and allowed.

    Returns (submitted_count, stats). ``submitted`` counts calls to ``trade_execute`` (risk may still block inside).
    """
    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    stats = LLMTradeRunStats()
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for llm-trade")

    client = build_sdk_client(settings)
    bal = get_balance_cents(client)
    risk = RiskManager(settings)
    ledger = DryRunLedger()

    resp = list_open_markets(client, limit=settings.trade_llm_max_markets_per_run)
    markets = list(getattr(resp, "markets", []) or [])
    stats.markets = len(markets)
    log.info(
        "llm_trade_scan_start",
        market_count=len(markets),
        execute=execute,
        trade_llm_auto_execute=settings.trade_llm_auto_execute,
        dry_run=settings.dry_run,
        live_trading=settings.live_trading,
    )
    if not markets:
        log.warning("llm_trade_no_open_markets", note="API returned zero open markets for this limit")

    for m in markets:
        s = summarize_market_row(m)
        ticker = s.ticker
        print(f"llm-trade: {ticker} …", flush=True)
        if settings.trade_min_market_volume is not None:
            vol = s.volume
            if vol is None or vol < settings.trade_min_market_volume:
                stats.skip_low_volume += 1
                log.info("llm_trade_skip_low_volume", ticker=ticker, volume=vol)
                continue
        try:
            ob = get_orderbook(client, ticker)
        except Exception as exc:  # noqa: BLE001
            stats.skip_orderbook += 1
            log.warning("llm_trade_skip_orderbook", ticker=ticker, error=str(exc))
            continue

        yb_c = best_yes_bid_cents(ob)
        nb_c = best_no_bid_cents(ob)
        if yb_c is None or nb_c is None:
            stats.skip_no_bids += 1
            log.info(
                "llm_trade_skip_no_bids",
                ticker=ticker,
                yes_bid_cents=yb_c,
                no_bid_cents=nb_c,
            )
            continue

        yes_bid_d = yb_c / 100.0
        yes_ask_d = implied_yes_ask_dollars(nb_c / 100.0)
        yes_ask_c = int(max(1, min(99, round(yes_ask_d * 100.0))))

        max_allowed = effective_max_contracts(
            settings, balance_cents=bal, yes_price_cents=yes_ask_c
        )

        verdict = llm_evaluate_opportunity(
            settings=settings,
            ticker=ticker,
            title=s.title,
            yes_bid_cents=yb_c,
            yes_ask_cents=yes_ask_c,
            yes_bid_dollars=yes_bid_d,
            yes_ask_dollars=yes_ask_d,
            balance_cents=bal,
            max_contracts_allowed=max_allowed,
        )
        if verdict is None:
            stats.llm_no_verdict += 1
            log.warning(
                "llm_trade_no_verdict",
                ticker=ticker,
                note="OpenAI returned no usable JSON (check OPENAI_API_KEY, model, network, SSL)",
            )
            continue

        log.info(
            "llm_verdict",
            ticker=ticker,
            approve=verdict.approve,
            buy_yes=verdict.buy_yes,
            fair_yes=verdict.fair_yes,
            reason=verdict.reason[:500],
        )

        if not verdict.approve or not verdict.buy_yes:
            stats.llm_declined += 1
            log.info(
                "llm_trade_skip_llm_declined",
                ticker=ticker,
                approve=verdict.approve,
                buy_yes=verdict.buy_yes,
            )
            continue

        count = max(1, min(verdict.contracts, max_allowed, settings.max_contracts_per_market))
        limit_c = max(1, min(99, min(verdict.limit_yes_price_cents, yes_ask_c)))

        if not _passes_bot_edge(
            settings,
            fair_yes=verdict.fair_yes,
            yes_bid_dollars=yes_bid_d,
            yes_ask_dollars=yes_ask_d,
            contracts=count,
        ):
            stats.bot_math_rejected += 1
            log.info("llm_trade_rejected_by_bot_math", ticker=ticker, fair_yes=verdict.fair_yes)
            continue

        intent = make_limit_intent(
            ticker=ticker,
            side="yes",
            action="buy",
            count=count,
            yes_price_cents=limit_c,
        )

        if not execute:
            stats.skipped_cli_no_execute += 1
            log.warning(
                "llm_trade_candidate",
                ticker=ticker,
                count=count,
                limit_yes_price_cents=limit_c,
                note="re-run with --execute and TRADE_LLM_AUTO_EXECUTE=true to submit",
            )
            continue

        if not settings.trade_llm_auto_execute:
            stats.blocked_trade_llm_auto_execute_false += 1
            log.warning(
                "llm_trade_blocked",
                ticker=ticker,
                reason="TRADE_LLM_AUTO_EXECUTE_false",
            )
            continue

        trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
        stats.submitted += 1

    return stats.submitted, stats
