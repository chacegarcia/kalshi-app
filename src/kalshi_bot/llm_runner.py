"""LLM-assisted market scan: model reasons about each market; we enforce bot math then optionally trade."""

from __future__ import annotations

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
    return True


def run_llm_opportunity_pipeline(
    settings: Settings,
    *,
    execute: bool,
    log: StructuredLogger | None = None,
) -> int:
    """Scan open markets, LLM verdict + bot math; optionally ``trade_execute`` when ``execute`` and allowed.

    Returns number of orders submitted (dry-run or live).
    """
    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for llm-trade")

    client = build_sdk_client(settings)
    bal = get_balance_cents(client)
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    submitted = 0

    resp = list_open_markets(client, limit=settings.trade_llm_max_markets_per_run)
    markets = list(getattr(resp, "markets", []) or [])

    for m in markets:
        s = summarize_market_row(m)
        ticker = s.ticker
        try:
            ob = get_orderbook(client, ticker)
        except Exception as exc:  # noqa: BLE001
            log.warning("llm_trade_skip_orderbook", ticker=ticker, error=str(exc))
            continue

        yb_c = best_yes_bid_cents(ob)
        nb_c = best_no_bid_cents(ob)
        if yb_c is None or nb_c is None:
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
            log.warning(
                "llm_trade_candidate",
                ticker=ticker,
                count=count,
                limit_yes_price_cents=limit_c,
                note="re-run with --execute and TRADE_LLM_AUTO_EXECUTE=true to submit",
            )
            continue

        if not settings.trade_llm_auto_execute:
            log.warning(
                "llm_trade_blocked",
                ticker=ticker,
                reason="TRADE_LLM_AUTO_EXECUTE_false",
            )
            continue

        trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
        submitted += 1

    return submitted
