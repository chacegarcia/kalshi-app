"""LLM-assisted market scan: model reasons about each market; we enforce bot math then optionally trade."""

from __future__ import annotations

import random
from dataclasses import dataclass

from kalshi_bot.config import Settings
from kalshi_bot.edge_math import (
    implied_no_ask_dollars,
    implied_yes_ask_dollars,
    min_edge_threshold_for_mid,
    net_edge_buy_no_long,
    net_edge_buy_yes_long,
)
from kalshi_bot.execution import DryRunLedger
from kalshi_bot.logger import StructuredLogger, get_logger, maybe_clear_structured_log_after_tickers
from kalshi_bot.market_data import (
    TapeUniverseEntry,
    build_llm_trade_open_universe,
    build_tape_universe_for_llm,
    fetch_open_markets_unique_up_to,
    fetch_yes_close_prices,
    get_orderbook,
    summarize_market_row,
    yes_bid_and_no_bid_cents_for_trading,
)
from kalshi_bot.momentum import momentum_buy_intent_if_hot
from kalshi_bot.portfolio import PortfolioSnapshot, fetch_portfolio_snapshot, get_balance_cents
from kalshi_bot.risk import RiskManager
from kalshi_bot.sizing import effective_max_contracts
from kalshi_bot.strategy import (
    choose_entry_side_and_ask_cents,
    entry_filter_timing_and_event,
    should_skip_buy_due_to_long_yes_cap,
    should_skip_buy_ticker_substrings,
    skip_buy_yes_longshot,
)
from kalshi_bot.trading import build_sdk_client, make_limit_intent, trade_execute
from kalshi_bot.log_insights import adaptive_edge_deltas_from_wl, aggregate_structured_log_tail
from kalshi_bot.llm_screen import LLMOpportunityVerdict, llm_evaluate_opportunity
from kalshi_bot.monitor import win_loss_snapshot


@dataclass
class LLMTradeRunStats:
    """Counts why markets did not become orders (see end-of-run summary)."""

    markets: int = 0
    tape_trades_fetched: int = 0
    skip_orderbook: int = 0
    skip_no_bids: int = 0
    llm_no_verdict: int = 0
    llm_declined: int = 0
    llm_fair_ask_override: int = 0
    bot_math_rejected: int = 0
    skip_low_volume: int = 0
    momentum_llm_bypass: int = 0
    momentum_candle_error: int = 0
    skipped_cli_no_execute: int = 0
    blocked_zero_contracts_balance: int = 0
    skip_zero_contracts_after_verdict: int = 0
    skip_yes_ask_longshot: int = 0
    skip_ticker_substring: int = 0
    skip_long_yes_cap: int = 0
    skip_theta_decay: int = 0
    skip_event_not_top_yes: int = 0
    skip_multi_choice_not_top_n: int = 0
    skip_multi_choice_below_min: int = 0
    skip_multi_choice_not_in_event: int = 0
    blocked_trade_llm_auto_execute_false: int = 0
    submitted: int = 0
    # Filled each run so the summary explains why nothing submitted without reading logs
    cli_execute: bool = False
    dry_run: bool = False
    live_trading: bool = False
    trade_llm_auto_execute: bool = False
    pipeline_error: str | None = None

    def lines(self) -> list[str]:
        out = [
            "--- llm-trade summary ---",
            f"  CLI --execute:                {self.cli_execute}   (must be true to attempt orders)",
            f"  TRADE_LLM_AUTO_EXECUTE:       {self.trade_llm_auto_execute}   (.env gate for real submits)",
            f"  DRY_RUN:                      {self.dry_run}   (true = never sends to exchange API)",
            f"  LIVE_TRADING:                 {self.live_trading}",
        ]
        if self.pipeline_error:
            out.append(f"  PIPELINE ERROR:               {self.pipeline_error}")
        out.extend(
            [
                f"  markets scanned:              {self.markets}",
                f"  public trades (tape mode):    {self.tape_trades_fetched}",
                f"  skip (orderbook error):       {self.skip_orderbook}",
                f"  skip (no YES/NO bids):        {self.skip_no_bids}",
                f"  LLM no JSON / API fail:       {self.llm_no_verdict}",
                f"  LLM declined (approve/buy):   {self.llm_declined}  <-- often high if model says no",
                f"  LLM decline overridden (fair≈ask): {self.llm_fair_ask_override}",
                f"  blocked by bot math (edge):   {self.bot_math_rejected}",
                f"  skip (volume below min):      {self.skip_low_volume}",
                f"  skip (balance→0 shares):      {self.blocked_zero_contracts_balance}",
                f"  skip (verdict size 0):        {self.skip_zero_contracts_after_verdict}",
                f"  skip (YES ask < min longshot): {self.skip_yes_ask_longshot}",
                f"  skip (ticker substring block):  {self.skip_ticker_substring}",
                f"  skip (long-YES family cap):     {self.skip_long_yes_cap}",
                f"  skip (theta / near-exp longshot): {self.skip_theta_decay}",
                f"  skip (not in event top-N YES):  {self.skip_event_not_top_yes}",
                f"  skip (multi-choice not top-N):    {self.skip_multi_choice_not_top_n}",
                f"  skip (multi-choice < min chance): {self.skip_multi_choice_below_min}",
                f"  skip (multi-choice ticker missing): {self.skip_multi_choice_not_in_event}",
                f"  momentum bypass (chart YES):  {self.momentum_llm_bypass}",
                f"  momentum candle fetch errors: {self.momentum_candle_error}",
                f"  skipped (--execute false):    {self.skipped_cli_no_execute}",
                f"  TRADE_LLM_AUTO_EXECUTE false: {self.blocked_trade_llm_auto_execute_false}",
                f"  reached trade_execute:        {self.submitted}",
                "  (If submitted > 0 but no fills: see structured logs for order_blocked / dry_run / risk.)",
                "---",
            ]
        )
        return out


def _llm_effective_edge_thresholds(
    settings: Settings,
    *,
    adaptive_min_add: float = 0.0,
    adaptive_mid_add: float = 0.0,
) -> tuple[float, float]:
    """LLM-specific overrides so llm-trade can be looser than tape/discover without changing global TRADE_*."""
    mn = settings.trade_llm_min_net_edge_after_fees
    me = settings.trade_llm_edge_middle_extra_edge
    base_mn = settings.trade_min_net_edge_after_fees if mn is None else mn
    base_me = settings.trade_edge_middle_extra_edge if me is None else me
    return (base_mn + adaptive_min_add, base_me + adaptive_mid_add)


def _passes_bot_edge(
    settings: Settings,
    *,
    fair_yes: float,
    yes_bid_dollars: float,
    yes_ask_dollars: float,
    contracts: int,
    min_net_edge: float | None = None,
    middle_extra_edge: float | None = None,
) -> bool:
    """Deterministic gate: LLM cannot bypass fee-aware edge + mid penalty."""
    base_min = settings.trade_min_net_edge_after_fees if min_net_edge is None else min_net_edge
    mid_boost = settings.trade_edge_middle_extra_edge if middle_extra_edge is None else middle_extra_edge
    edge = net_edge_buy_yes_long(fair_yes=fair_yes, yes_ask_dollars=yes_ask_dollars, contracts=contracts)
    mid = (yes_bid_dollars + yes_ask_dollars) / 2.0
    need = min_edge_threshold_for_mid(
        mid,
        base_min_edge=base_min,
        middle_extra=mid_boost,
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


def _passes_bot_edge_no(
    settings: Settings,
    *,
    fair_no: float,
    no_bid_dollars: float,
    no_ask_dollars: float,
    contracts: int,
    min_net_edge: float | None = None,
    middle_extra_edge: float | None = None,
) -> bool:
    """Fee-aware edge gate for buy NO (same thresholds as YES)."""
    base_min = settings.trade_min_net_edge_after_fees if min_net_edge is None else min_net_edge
    mid_boost = settings.trade_edge_middle_extra_edge if middle_extra_edge is None else middle_extra_edge
    edge = net_edge_buy_no_long(fair_no=fair_no, no_ask_dollars=no_ask_dollars, contracts=contracts)
    mid = (no_bid_dollars + no_ask_dollars) / 2.0
    need = min_edge_threshold_for_mid(
        mid,
        base_min_edge=base_min,
        middle_extra=mid_boost,
    )
    if edge < need:
        return False
    if no_ask_dollars > settings.strategy_max_yes_ask_dollars:
        return False
    spread = max(0.0, no_ask_dollars - no_bid_dollars)
    if spread < settings.strategy_min_spread_dollars:
        return False
    if settings.trade_max_entry_spread_dollars is not None and spread > settings.trade_max_entry_spread_dollars:
        return False
    return True


def _passes_bot_math_for_llm(
    settings: Settings,
    *,
    fair_yes: float,
    yes_bid_dollars: float,
    yes_ask_dollars: float,
    no_bid_dollars: float,
    no_ask_dollars: float,
    entry_side: str,
    contracts: int,
    llm_decline_overridden: bool,
    adaptive_min_add: float = 0.0,
    adaptive_mid_add: float = 0.0,
) -> bool:
    """Strict fee-edge check, or when LLM declined but fair is near ask, only book-quality checks."""
    if entry_side == "no":
        spread = max(0.0, no_ask_dollars - no_bid_dollars)
        if no_ask_dollars > settings.strategy_max_yes_ask_dollars:
            return False
        if spread < settings.strategy_min_spread_dollars:
            return False
        if settings.trade_max_entry_spread_dollars is not None and spread > settings.trade_max_entry_spread_dollars:
            return False
        fair_no = 1.0 - fair_yes
        if (
            llm_decline_overridden
            and settings.trade_llm_accept_when_fair_covers_ask
            and fair_no >= no_ask_dollars - settings.trade_llm_fair_ask_slippage
        ):
            return True
        mn, me = _llm_effective_edge_thresholds(
            settings, adaptive_min_add=adaptive_min_add, adaptive_mid_add=adaptive_mid_add
        )
        return _passes_bot_edge_no(
            settings,
            fair_no=fair_no,
            no_bid_dollars=no_bid_dollars,
            no_ask_dollars=no_ask_dollars,
            contracts=contracts,
            min_net_edge=mn,
            middle_extra_edge=me,
        )

    spread = max(0.0, yes_ask_dollars - yes_bid_dollars)
    if yes_ask_dollars > settings.strategy_max_yes_ask_dollars:
        return False
    if spread < settings.strategy_min_spread_dollars:
        return False
    if settings.trade_max_entry_spread_dollars is not None and spread > settings.trade_max_entry_spread_dollars:
        return False

    if (
        llm_decline_overridden
        and settings.trade_llm_accept_when_fair_covers_ask
        and fair_yes >= yes_ask_dollars - settings.trade_llm_fair_ask_slippage
    ):
        return True

    mn, me = _llm_effective_edge_thresholds(
        settings, adaptive_min_add=adaptive_min_add, adaptive_mid_add=adaptive_mid_add
    )
    return _passes_bot_edge(
        settings,
        fair_yes=fair_yes,
        yes_bid_dollars=yes_bid_dollars,
        yes_ask_dollars=yes_ask_dollars,
        contracts=contracts,
        min_net_edge=mn,
        middle_extra_edge=me,
    )


def run_llm_opportunity_pipeline(
    settings: Settings,
    *,
    execute: bool,
    log: StructuredLogger | None = None,
    use_tape_universe: bool = False,
) -> tuple[int, LLMTradeRunStats]:
    """Scan open markets **or** tape-ranked tickers; LLM verdict + bot math; optional ``trade_execute``.

    Returns (submitted_count, stats). ``submitted`` counts calls to ``trade_execute`` (risk may still block inside).
    """
    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    stats = LLMTradeRunStats()
    stats.cli_execute = execute
    stats.dry_run = settings.dry_run
    stats.live_trading = settings.live_trading
    stats.trade_llm_auto_execute = settings.trade_llm_auto_execute
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for llm-trade")

    client = build_sdk_client(settings)
    bal = get_balance_cents(client)
    risk = RiskManager(settings)
    ledger = DryRunLedger()

    rows: list[tuple[str, str]] = []
    open_volumes: dict[str, int | None] = {}
    tape_entries: list[TapeUniverseEntry] | None = None
    if use_tape_universe:
        tape_entries, stats.tape_trades_fetched = build_tape_universe_for_llm(
            client,
            max_trades_fetch=settings.trade_tape_max_trades_fetch,
            top_markets=settings.trade_tape_top_markets,
            min_flow_usd=settings.trade_tape_min_flow_usd,
            min_market_volume=settings.trade_min_market_volume,
        )
        rows = [(e.ticker, e.title) for e in tape_entries]
        stats.markets = len(rows)
        log.info(
            "llm_trade_scan_start",
            universe="tape",
            market_count=len(rows),
            tape_trades_fetched=stats.tape_trades_fetched,
            execute=execute,
            trade_llm_auto_execute=settings.trade_llm_auto_execute,
            dry_run=settings.dry_run,
            live_trading=settings.live_trading,
        )
        if not rows:
            log.warning("llm_trade_tape_empty", note="no tickers after tape rank + filters")
        else:
            print(
                f"llm-trade: tape mode — {len(rows)} markets after flow rank "
                f"({stats.tape_trades_fetched} public prints in fetch window)",
                flush=True,
            )
    else:
        skip_pages = random.randint(0, max(0, settings.trade_llm_random_skip_pages_max))
        btc_priority = bool(
            settings.trade_llm_bitcoin_priority_enabled
            and settings.trade_llm_bitcoin_priority_max_markets > 0
            and (settings.trade_llm_bitcoin_priority_prefix or "").strip()
        )
        if btc_priority:
            summaries = build_llm_trade_open_universe(
                client,
                target_count=settings.trade_llm_max_markets_per_run,
                max_pages=settings.trade_llm_open_markets_max_pages,
                mve_filter="exclude",
                leading_pages_to_skip=skip_pages,
                bitcoin_prefix=settings.trade_llm_bitcoin_priority_prefix,
                bitcoin_max_markets=settings.trade_llm_bitcoin_priority_max_markets,
            )
        else:
            summaries = fetch_open_markets_unique_up_to(
                client,
                target_count=settings.trade_llm_max_markets_per_run,
                mve_filter="exclude",
                max_pages=settings.trade_llm_open_markets_max_pages,
                leading_pages_to_skip=skip_pages,
            )
        for s in summaries:
            rows.append((s.ticker, s.title))
            open_volumes[s.ticker] = s.volume
        if settings.trade_llm_shuffle_open_markets:
            if btc_priority:
                pfx = (settings.trade_llm_bitcoin_priority_prefix or "").strip().upper()
                head = [(t, ti) for t, ti in rows if t.upper().startswith(pfx)]
                tail = [(t, ti) for t, ti in rows if not t.upper().startswith(pfx)]
                random.shuffle(tail)
                rows = head + tail
            else:
                random.shuffle(rows)
        stats.markets = len(rows)
        mode = "BTC-prefix + open mix" if btc_priority else "open only"
        print(
            f"llm-trade: {len(rows)} distinct open markets ({mode}; skip {skip_pages} pages; shuffled={settings.trade_llm_shuffle_open_markets})",
            flush=True,
        )
        log.info(
            "llm_trade_scan_start",
            universe="open",
            market_count=len(rows),
            llm_open_list_skip_pages=skip_pages,
            llm_bitcoin_priority=btc_priority,
            execute=execute,
            trade_llm_auto_execute=settings.trade_llm_auto_execute,
            dry_run=settings.dry_run,
            live_trading=settings.live_trading,
        )
        if not rows:
            log.warning("llm_trade_no_open_markets", note="API returned zero open markets after pagination")

    wl_snap = win_loss_snapshot()
    ad_min, ad_mid, wl_stress, wl_note = adaptive_edge_deltas_from_wl(
        wl_snap,
        enabled=settings.trade_llm_adapt_to_session_wl,
        min_closed=settings.trade_llm_adapt_min_closed_trades,
    )
    log.info(
        "llm_trade_session_adaptive",
        wins=wl_snap.get("wins", 0),
        losses=wl_snap.get("losses", 0),
        ties=wl_snap.get("ties", 0),
        extra_min_net_edge=ad_min,
        extra_mid_edge=ad_mid,
        stress=wl_stress,
    )
    if ad_min > 0 or ad_mid > 0 or wl_note:
        print(
            f"llm-trade: session W–L {wl_snap.get('wins', 0)}-{wl_snap.get('losses', 0)} "
            f"→ adaptive edge bump min={ad_min:.4f} mid={ad_mid:.4f} stress={wl_stress}",
            flush=True,
        )
    else:
        print(
            f"llm-trade: session W–L {wl_snap.get('wins', 0)}-{wl_snap.get('losses', 0)} "
            "(no adaptation yet or disabled)",
            flush=True,
        )

    snap_for_cap: PortfolioSnapshot | None = None
    if settings.trade_entry_cap_long_yes_max > 0 and (settings.trade_entry_cap_long_yes_substring or "").strip():
        try:
            snap_for_cap = fetch_portfolio_snapshot(client, ticker=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("llm_trade_portfolio_snapshot_fail", error=str(exc))
            snap_for_cap = None

    event_data_cache: dict[str, list[tuple[str, float]] | None] = {}

    for i, (ticker, title) in enumerate(rows):
        try:
            te = tape_entries[i] if tape_entries is not None else None
            if te:
                print(
                    f"llm-trade: {ticker} (tape flow≈${te.flow_usd_approx:.2f}, rank #{te.rank}/{len(tape_entries)}) …",
                    flush=True,
                )
                log.info(
                    "llm_trade_tape_row",
                    ticker=ticker,
                    flow_usd_approx=te.flow_usd_approx,
                    public_trade_count=te.public_trade_count,
                    rank=te.rank,
                    tape_universe_size=len(tape_entries),
                )
            else:
                print(f"llm-trade: {ticker} …", flush=True)
            if not use_tape_universe and settings.trade_min_market_volume is not None:
                vol = open_volumes.get(ticker)
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

            yb_c, nb_c = yes_bid_and_no_bid_cents_for_trading(ob)
            if nb_c is None:
                stats.skip_no_bids += 1
                log.info(
                    "llm_trade_skip_no_bids",
                    ticker=ticker,
                    yes_bid_cents=yb_c,
                    no_bid_cents=nb_c,
                    note="no NO bids; cannot imply YES ask",
                )
                continue

            yes_bid_d = yb_c / 100.0
            yes_ask_d = implied_yes_ask_dollars(nb_c / 100.0)
            yes_ask_c = int(max(1, min(99, round(yes_ask_d * 100.0))))
            no_bid_d = nb_c / 100.0
            no_ask_d = implied_no_ask_dollars(yb_c / 100.0)
            no_ask_c = int(max(1, min(99, round(no_ask_d * 100.0))))
            entry_side, chosen_ask_c = choose_entry_side_and_ask_cents(
                settings, yes_ask_cents=yes_ask_c, yes_bid_cents=yb_c, no_bid_cents=nb_c
            )

            if should_skip_buy_ticker_substrings(settings, ticker):
                stats.skip_ticker_substring += 1
                log.info("llm_trade_skip_ticker_substring", ticker=ticker)
                continue
            if snap_for_cap is not None and should_skip_buy_due_to_long_yes_cap(
                settings, ticker=ticker, snap=snap_for_cap
            ):
                stats.skip_long_yes_cap += 1
                log.info(
                    "llm_trade_skip_long_yes_cap",
                    ticker=ticker,
                    cap=settings.trade_entry_cap_long_yes_max,
                    substring=(settings.trade_entry_cap_long_yes_substring or "").strip(),
                )
                continue

            if skip_buy_yes_longshot(settings, chosen_ask_c):
                stats.skip_yes_ask_longshot += 1
                log.info(
                    "llm_trade_skip_longshot_yes",
                    ticker=ticker,
                    yes_ask_cents=yes_ask_c,
                    chosen_ask_cents=chosen_ask_c,
                    entry_side=entry_side,
                    min_yes_ask_cents=settings.trade_entry_effective_min_yes_ask_cents,
                )
                continue

            skip_te, te_reason = entry_filter_timing_and_event(
                settings, client, ticker, chosen_ask_c, event_data_cache
            )
            if skip_te:
                if te_reason == "theta_decay_longshot":
                    stats.skip_theta_decay += 1
                elif te_reason == "not_in_event_top_yes":
                    stats.skip_event_not_top_yes += 1
                elif te_reason == "multi_choice_not_top_n":
                    stats.skip_multi_choice_not_top_n += 1
                elif te_reason == "multi_choice_below_min_chance":
                    stats.skip_multi_choice_below_min += 1
                elif te_reason == "multi_choice_ticker_not_in_event":
                    stats.skip_multi_choice_not_in_event += 1
                log.info(
                    "llm_trade_skip_entry_filter",
                    ticker=ticker,
                    reason=te_reason,
                    yes_ask_cents=yes_ask_c,
                    chosen_ask_cents=chosen_ask_c,
                    entry_side=entry_side,
                )
                continue

            max_allowed = effective_max_contracts(
                settings, balance_cents=bal, yes_price_cents=chosen_ask_c
            )
            if max_allowed < 1:
                stats.blocked_zero_contracts_balance += 1
                log.info(
                    "llm_trade_skip_zero_contracts_balance",
                    ticker=ticker,
                    balance_cents=bal,
                    yes_ask_cents=yes_ask_c,
                    chosen_ask_cents=chosen_ask_c,
                    entry_side=entry_side,
                )
                continue

            if entry_side == "yes" and settings.trade_momentum_enabled and settings.trade_momentum_llm_bypass:
                try:
                    closes = fetch_yes_close_prices(
                        client,
                        ticker,
                        period_interval_minutes=settings.trade_momentum_period_minutes,
                        lookback_seconds=settings.trade_momentum_lookback_minutes * 60,
                    )
                except Exception as exc:  # noqa: BLE001
                    stats.momentum_candle_error += 1
                    log.warning("llm_momentum_candles_fail", ticker=ticker, error=str(exc))
                    closes = []
                if closes:
                    mintent, mwhy = momentum_buy_intent_if_hot(
                        ticker=ticker,
                        yes_bid_dollars=yes_bid_d,
                        yes_ask_dollars=yes_ask_d,
                        settings=settings,
                        close_prices=closes,
                    )
                    if mintent is not None:
                        cnt = min(mintent.count, max_allowed)
                        if wl_stress:
                            cnt = min(cnt, 1)
                        if cnt < 1:
                            continue
                        mintent = make_limit_intent(
                            ticker=ticker,
                            side="yes",
                            action="buy",
                            count=cnt,
                            yes_price_cents=mintent.yes_price_cents,
                        )
                        log.info("llm_trade_momentum_bypass", ticker=ticker, detail=mwhy, count=cnt)
                        print(f"llm-trade: {ticker} momentum bypass — {mwhy}", flush=True)
                        if not execute:
                            stats.skipped_cli_no_execute += 1
                            log.warning(
                                "llm_trade_momentum_candidate",
                                ticker=ticker,
                                count=cnt,
                                yes_price_cents=mintent.yes_price_cents,
                                note="re-run with --execute and TRADE_LLM_AUTO_EXECUTE=true",
                            )
                        elif not settings.trade_llm_auto_execute:
                            stats.blocked_trade_llm_auto_execute_false += 1
                            log.warning(
                                "llm_trade_momentum_blocked",
                                ticker=ticker,
                                reason="TRADE_LLM_AUTO_EXECUTE_false",
                            )
                        else:
                            trade_execute(
                                client=client, settings=settings, risk=risk, log=log, intent=mintent, ledger=ledger
                            )
                            stats.momentum_llm_bypass += 1
                            stats.submitted += 1
                        continue

            verdict = llm_evaluate_opportunity(
                settings=settings,
                ticker=ticker,
                title=title,
                yes_bid_cents=yb_c,
                yes_ask_cents=yes_ask_c,
                yes_bid_dollars=yes_bid_d,
                yes_ask_dollars=yes_ask_d,
                balance_cents=bal,
                max_contracts_allowed=max_allowed,
                adaptive_extra_min_net_edge=ad_min,
                adaptive_extra_mid_edge=ad_mid,
                session_performance_note=wl_note,
                tape_flow_usd_approx=te.flow_usd_approx if te else None,
                tape_rank=te.rank if te else None,
                tape_public_trade_count=te.public_trade_count if te else None,
                tape_universe_size=len(tape_entries) if te else None,
                no_bid_cents=nb_c,
                no_ask_cents=no_ask_c,
                no_bid_dollars=no_bid_d,
                no_ask_dollars=no_ask_d,
                entry_side=entry_side,
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

            llm_decline_overridden = False
            if not verdict.approve or (entry_side == "yes" and not verdict.buy_yes):
                slip = settings.trade_llm_fair_ask_slippage
                if settings.trade_llm_accept_when_fair_covers_ask:
                    if entry_side == "yes" and verdict.fair_yes >= yes_ask_d - slip:
                        llm_decline_overridden = True
                    elif entry_side == "no" and (1.0 - verdict.fair_yes) >= no_ask_d - slip:
                        llm_decline_overridden = True
                if llm_decline_overridden:
                    stats.llm_fair_ask_override += 1
                    log.info(
                        "llm_trade_decline_overridden",
                        ticker=ticker,
                        fair_yes=verdict.fair_yes,
                        entry_side=entry_side,
                        yes_ask_dollars=yes_ask_d,
                        no_ask_dollars=no_ask_d,
                        slippage=slip,
                        reason=verdict.reason[:300],
                    )
                else:
                    stats.llm_declined += 1
                    log.info(
                        "llm_trade_skip_llm_declined",
                        ticker=ticker,
                        approve=verdict.approve,
                        buy_yes=verdict.buy_yes,
                        entry_side=entry_side,
                    )
                    continue

            count = min(verdict.contracts, max_allowed)
            if count < 1:
                stats.skip_zero_contracts_after_verdict += 1
                log.info("llm_trade_skip_zero_contracts_after_cap", ticker=ticker, verdict_contracts=verdict.contracts)
                continue
            if wl_stress:
                count = min(count, 1)
            if entry_side == "yes":
                limit_c = max(1, min(99, min(verdict.limit_yes_price_cents, yes_ask_c)))
            else:
                limit_c = max(1, min(99, no_ask_c))
            eff_floor = settings.trade_entry_effective_min_yes_ask_cents
            if eff_floor > 0:
                limit_c = max(limit_c, eff_floor)
                limit_c = min(limit_c, yes_ask_c if entry_side == "yes" else no_ask_c)

            if not _passes_bot_math_for_llm(
                settings,
                fair_yes=verdict.fair_yes,
                yes_bid_dollars=yes_bid_d,
                yes_ask_dollars=yes_ask_d,
                no_bid_dollars=no_bid_d,
                no_ask_dollars=no_ask_d,
                entry_side=entry_side,
                contracts=count,
                llm_decline_overridden=llm_decline_overridden,
                adaptive_min_add=ad_min,
                adaptive_mid_add=ad_mid,
            ):
                stats.bot_math_rejected += 1
                log.info(
                    "llm_trade_rejected_by_bot_math",
                    ticker=ticker,
                    fair_yes=verdict.fair_yes,
                    entry_side=entry_side,
                )
                continue

            intent = make_limit_intent(
                ticker=ticker,
                side=entry_side,
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
        finally:
            maybe_clear_structured_log_after_tickers(
                log_path=settings.structured_log_path,
                every_n=settings.structured_log_clear_every_n_tickers,
                processed_count=i + 1,
                log=log,
            )

    tail = aggregate_structured_log_tail(settings.structured_log_path)
    log.info(
        "llm_trade_log_tail_summary",
        lines_parsed=tail.get("lines_parsed"),
        top_events=tail.get("top_events"),
        order_blocked_reasons=tail.get("order_blocked_reasons"),
    )
    print("--- structured log tail (event counts) ---", flush=True)
    for line in tail.get("top_events", [])[:12]:
        print(f"  {line}", flush=True)

    return stats.submitted, stats
