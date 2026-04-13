"""Kalshi crypto binary contracts (BTC/ETH series): public spot reference + REST order book / candles.

Uses the same entry rules as tape-trade. Discovery: ``TRADE_CRYPTO_KALSHI_PREFIXES`` (e.g. ``KXBTC,KXETH``) or
legacy ``TRADE_BITCOIN_TICKER_PREFIX``, or default ``KXBTC``. Pin ``TRADE_BITCOIN_KALSHI_TICKER`` for one market.
Spot reference: ``TRADE_CRYPTO_SPOT_PRICE_SOURCE`` (auto / coingecko / binance).
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.btc_price import fetch_crypto_spot_usd_for_kalshi_ticker
from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings
from kalshi_bot.edge_math import implied_no_ask_dollars, implied_yes_ask_dollars
from kalshi_bot.execution import DryRunLedger
from kalshi_bot.logger import StructuredLogger, get_logger
from kalshi_bot.market_data import (
    fetch_open_markets_by_ticker_prefixes,
    fetch_yes_close_prices,
    get_market,
    get_orderbook,
    summarize_market_row,
    yes_bid_and_no_bid_cents_for_trading,
)
from kalshi_bot.momentum import momentum_buy_intent_if_hot
from kalshi_bot.portfolio import PortfolioSnapshot, fetch_portfolio_snapshot
from kalshi_bot.risk import RiskManager
from kalshi_bot.strategy import (
    TradeIntent,
    choose_entry_side_and_ask_cents,
    entry_filter_timing_and_event,
    should_skip_buy_due_to_long_yes_cap,
    should_skip_buy_ticker_substrings,
    signal_edge_buy_no_from_ticker,
    signal_edge_buy_yes_from_ticker,
    signal_from_bar,
    signal_from_bar_buy_no,
    skip_buy_yes_longshot,
)
from kalshi_bot.trading import build_sdk_client, trade_execute


@dataclass
class BitcoinTradeRunStats:
    btc_price_ok: int = 0
    btc_price_fail: int = 0
    skip_low_volume: int = 0
    skip_orderbook: int = 0
    skip_no_bids: int = 0
    skip_yes_ask_longshot: int = 0
    skip_ticker_substring: int = 0
    skip_long_yes_cap: int = 0
    skip_theta_decay: int = 0
    skip_resolution_too_far: int = 0
    skip_event_not_top_yes: int = 0
    skip_multi_choice_not_top_n: int = 0
    skip_multi_choice_below_min: int = 0
    skip_multi_choice_not_in_event: int = 0
    no_rule_signal: int = 0
    momentum_signal: int = 0
    momentum_candle_error: int = 0
    skipped_cli_no_execute: int = 0
    blocked_auto_execute_false: int = 0
    submitted: int = 0

    def lines(self) -> list[str]:
        return [
            "--- bitcoin-trade summary ---",
            f"  BTC/USD spot fetches OK:           {self.btc_price_ok}",
            f"  BTC/USD spot fetch failed:         {self.btc_price_fail}",
            f"  skip (volume below min):            {self.skip_low_volume}",
            f"  skip (orderbook error):             {self.skip_orderbook}",
            f"  skip (no YES/NO bids):              {self.skip_no_bids}",
            f"  skip (YES ask < min / longshot):    {self.skip_yes_ask_longshot}",
            f"  skip (ticker substring block):      {self.skip_ticker_substring}",
            f"  skip (long-YES family cap):         {self.skip_long_yes_cap}",
            f"  skip (theta / near-exp longshot):   {self.skip_theta_decay}",
            f"  skip (resolution > max horizon):    {self.skip_resolution_too_far}",
            f"  skip (not in event top-N YES):      {self.skip_event_not_top_yes}",
            f"  skip (multi-choice not top-N):      {self.skip_multi_choice_not_top_n}",
            f"  skip (multi-choice < min chance):   {self.skip_multi_choice_below_min}",
            f"  skip (multi-choice ticker missing):  {self.skip_multi_choice_not_in_event}",
            f"  momentum (chart YES) signals:       {self.momentum_signal}",
            f"  momentum candle fetch errors:       {self.momentum_candle_error}",
            f"  no signal from .env rules:          {self.no_rule_signal}",
            f"  skipped (--execute false):          {self.skipped_cli_no_execute}",
            f"  TRADE_BITCOIN_AUTO_EXECUTE false:   {self.blocked_auto_execute_false}",
            f"  reached trade_execute:              {self.submitted}",
            "---",
        ]


def bitcoin_markets_configured(settings: Settings) -> bool:
    """True if user pinned a ticker or set crypto prefix discovery (``TRADE_CRYPTO_*`` / ``TRADE_BITCOIN_*``)."""
    if (settings.trade_bitcoin_kalshi_ticker or "").strip():
        return True
    if (settings.trade_crypto_kalshi_prefixes or "").strip():
        return True
    return bool((settings.trade_bitcoin_ticker_prefix or "").strip())


def crypto_kalshi_prefixes_for_discovery(settings: Settings) -> list[str]:
    """Non-empty list of Kalshi ticker prefixes for rotating crypto discovery."""
    raw = (settings.trade_crypto_kalshi_prefixes or "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    legacy = (settings.trade_bitcoin_ticker_prefix or "").strip()
    if legacy:
        return [legacy]
    return ["KXBTC"]


def resolve_bitcoin_candidate_tickers(client: KalshiSdkClient, settings: Settings) -> list[str]:
    """Pinned single ticker, or open markets matching ``TRADE_CRYPTO_KALSHI_PREFIXES`` (union) or legacy single prefix."""
    pinned = (settings.trade_bitcoin_kalshi_ticker or "").strip()
    if pinned:
        return [pinned]
    prefixes = crypto_kalshi_prefixes_for_discovery(settings)
    if not prefixes:
        return []
    rows = fetch_open_markets_by_ticker_prefixes(
        client,
        prefixes=prefixes,
        max_results=settings.trade_bitcoin_max_universe,
        max_api_pages=settings.trade_bitcoin_discovery_max_pages,
    )
    return [r.ticker for r in rows]


def pick_next_bitcoin_ticker(candidates: list[str], rotation_counter: list[int]) -> str | None:
    """Round-robin among ``candidates``; mutates ``rotation_counter[0]``."""
    if not candidates:
        return None
    idx = rotation_counter[0] % len(candidates)
    rotation_counter[0] += 1
    return candidates[idx]


def run_bitcoin_sidecar_if_due(
    settings: Settings,
    *,
    client: KalshiSdkClient,
    risk: RiskManager,
    ledger: DryRunLedger,
    log: StructuredLogger,
    execute: bool,
    scan_counter: list[int],
    rotation_counter: list[int] | None = None,
    log_prefix: str = "tape-trade",
) -> tuple[int, int]:
    """If ``scan_counter[0]`` is a multiple of ``TRADE_BITCOIN_EVERY_N_TICKER_SCANS``, evaluate one BTC contract.

    Returns ``(sidecar_runs, orders_submitted)`` — ``(0, 0)`` when the sidecar does not run.
    """
    if not settings.trade_bitcoin_sidecar_enabled or not bitcoin_markets_configured(settings):
        return 0, 0
    n_every = max(1, settings.trade_bitcoin_every_n_ticker_scans)
    if scan_counter[0] % n_every != 0:
        return 0, 0
    rot = rotation_counter if rotation_counter is not None else [0]
    print(
        f"{log_prefix}: bitcoin sidecar (ticker scan #{scan_counter[0]}, every {n_every}) …",
        flush=True,
    )
    candidates = resolve_bitcoin_candidate_tickers(client, settings)
    if not candidates:
        log.warning(
            "bitcoin_sidecar_no_open_markets",
            prefixes=crypto_kalshi_prefixes_for_discovery(settings),
            note="Set TRADE_BITCOIN_KALSHI_TICKER to pin one market, or TRADE_CRYPTO_KALSHI_PREFIXES / TRADE_BITCOIN_TICKER_PREFIX",
        )
        return 0, 0
    chosen = pick_next_bitcoin_ticker(candidates, rot)
    if chosen is None:
        return 0, 0
    bn, _bst = evaluate_bitcoin_ticker_pass(
        settings,
        client=client,
        risk=risk,
        ledger=ledger,
        log=log,
        execute=execute,
        market_ticker=chosen,
    )
    return 1, bn


def evaluate_bitcoin_ticker_pass(
    settings: Settings,
    *,
    client: KalshiSdkClient,
    risk: RiskManager,
    ledger: DryRunLedger,
    log: StructuredLogger,
    execute: bool,
    market_ticker: str | None = None,
) -> tuple[int, BitcoinTradeRunStats]:
    """REST + CoinGecko; ``market_ticker`` overrides ``TRADE_BITCOIN_KALSHI_TICKER`` when set."""
    stats = BitcoinTradeRunStats()

    ticker = (market_ticker or settings.trade_bitcoin_kalshi_ticker or "").strip()
    if not ticker:
        return 0, stats

    title = ticker
    vol: int | None = None
    try:
        mrow = get_market(client, ticker=ticker)
        m = getattr(mrow, "market", None)
        if m is not None:
            s = summarize_market_row(m)
            title = s.title or ticker
            vol = s.volume
    except Exception:  # noqa: BLE001
        pass

    ref_spot, spot_label = fetch_crypto_spot_usd_for_kalshi_ticker(
        ticker,
        settings.trade_crypto_spot_price_source,
    )
    if ref_spot is not None:
        stats.btc_price_ok = 1
    else:
        stats.btc_price_fail = 1

    spot_s = f"${ref_spot:,.2f}" if ref_spot is not None else "n/a"
    print(
        f"bitcoin-trade: ref spot ≈ {spot_s} USD ({spot_label}) | {ticker} — {title[:100]}",
        flush=True,
    )
    log.info(
        "bitcoin_trade_start",
        kalshi_ticker=ticker,
        btc_usd_spot=ref_spot,
        crypto_spot_label=spot_label,
        crypto_spot_source=settings.trade_crypto_spot_price_source,
        title=title[:200],
        execute=execute,
        trade_bitcoin_auto_execute=settings.trade_bitcoin_auto_execute,
        dry_run=settings.dry_run,
        live_trading=settings.live_trading,
    )

    if settings.trade_min_market_volume is not None:
        if vol is None or vol < settings.trade_min_market_volume:
            stats.skip_low_volume += 1
            log.info("bitcoin_skip_low_volume", ticker=ticker, volume=vol)
            return 0, stats

    snap_for_cap: PortfolioSnapshot | None = None
    if settings.trade_entry_cap_long_yes_max > 0 and (settings.trade_entry_cap_long_yes_substring or "").strip():
        try:
            snap_for_cap = fetch_portfolio_snapshot(client, ticker=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("bitcoin_portfolio_snapshot_fail", error=str(exc))
            snap_for_cap = None

    event_data_cache: dict[str, list[tuple[str, float]] | None] = {}

    try:
        ob = get_orderbook(client, ticker)
    except Exception as exc:  # noqa: BLE001
        stats.skip_orderbook += 1
        log.warning("bitcoin_skip_orderbook", ticker=ticker, error=str(exc))
        return 0, stats

    yb_c, nb_c = yes_bid_and_no_bid_cents_for_trading(ob)
    if nb_c is None:
        stats.skip_no_bids += 1
        return 0, stats

    yes_bid_d = yb_c / 100.0
    yes_ask_d = implied_yes_ask_dollars(nb_c / 100.0)
    yes_ask_c = int(max(1, min(99, round(yes_ask_d * 100.0))))
    no_bid_d = nb_c / 100.0
    no_ask_d = implied_no_ask_dollars(yb_c / 100.0)
    entry_side, chosen_ask_c = choose_entry_side_and_ask_cents(
        settings, yes_ask_cents=yes_ask_c, yes_bid_cents=yb_c, no_bid_cents=nb_c
    )
    if should_skip_buy_ticker_substrings(settings, ticker):
        stats.skip_ticker_substring += 1
        log.info("bitcoin_skip_ticker_substring", ticker=ticker)
        return 0, stats
    if snap_for_cap is not None and should_skip_buy_due_to_long_yes_cap(
        settings, ticker=ticker, snap=snap_for_cap
    ):
        stats.skip_long_yes_cap += 1
        log.info(
            "bitcoin_skip_long_yes_cap",
            ticker=ticker,
            cap=settings.trade_entry_cap_long_yes_max,
            substring=(settings.trade_entry_cap_long_yes_substring or "").strip(),
        )
        return 0, stats
    if skip_buy_yes_longshot(settings, chosen_ask_c):
        stats.skip_yes_ask_longshot += 1
        log.info(
            "bitcoin_skip_longshot_yes",
            ticker=ticker,
            yes_ask_cents=yes_ask_c,
            chosen_ask_cents=chosen_ask_c,
            entry_side=entry_side,
            min_yes_ask_cents=settings.trade_entry_effective_min_yes_ask_cents,
        )
        return 0, stats

    skip_te, te_reason = entry_filter_timing_and_event(
        settings, client, ticker, chosen_ask_c, event_data_cache
    )
    if skip_te:
        if te_reason == "theta_decay_longshot":
            stats.skip_theta_decay += 1
        elif te_reason == "resolution_too_far":
            stats.skip_resolution_too_far += 1
        elif te_reason == "not_in_event_top_yes":
            stats.skip_event_not_top_yes += 1
        elif te_reason == "multi_choice_not_top_n":
            stats.skip_multi_choice_not_top_n += 1
        elif te_reason == "multi_choice_below_min_chance":
            stats.skip_multi_choice_below_min += 1
        elif te_reason == "multi_choice_ticker_not_in_event":
            stats.skip_multi_choice_not_in_event += 1
        log.info(
            "bitcoin_skip_entry_filter",
            ticker=ticker,
            reason=te_reason,
            yes_ask_cents=yes_ask_c,
            chosen_ask_cents=chosen_ask_c,
            entry_side=entry_side,
        )
        return 0, stats

    intent: TradeIntent | None = None
    if entry_side == "yes" and settings.trade_momentum_enabled:
        try:
            closes = fetch_yes_close_prices(
                client,
                ticker,
                period_interval_minutes=settings.trade_momentum_period_minutes,
                lookback_seconds=settings.trade_momentum_lookback_minutes * 60,
            )
        except Exception as exc:  # noqa: BLE001
            stats.momentum_candle_error += 1
            log.warning("bitcoin_momentum_candles_fail", ticker=ticker, error=str(exc))
            closes = []
        if closes:
            intent, _mwhy = momentum_buy_intent_if_hot(
                ticker=ticker,
                yes_bid_dollars=yes_bid_d,
                yes_ask_dollars=yes_ask_d,
                settings=settings,
                close_prices=closes,
            )
            if intent is not None:
                stats.momentum_signal += 1
                log.info(
                    "bitcoin_momentum_signal",
                    ticker=ticker,
                    note=_mwhy,
                    ref_spot_usd=ref_spot,
                    crypto_spot_label=spot_label,
                )

    if intent is None and settings.trade_use_edge_strategy and settings.trade_fair_yes_prob is not None:
        if entry_side == "yes":
            intent = signal_edge_buy_yes_from_ticker(
                ticker=ticker,
                yes_bid_dollars=yes_bid_d,
                yes_ask_dollars=yes_ask_d,
                settings=settings,
            )
        else:
            intent = signal_edge_buy_no_from_ticker(
                ticker=ticker,
                no_bid_dollars=no_bid_d,
                no_ask_dollars=no_ask_d,
                settings=settings,
            )
    elif intent is None:
        if entry_side == "yes":
            intent = signal_from_bar(
                ticker=ticker,
                yes_bid_dollars=yes_bid_d,
                yes_ask_dollars=yes_ask_d,
                max_yes_ask_dollars=settings.trade_entry_effective_max_yes_ask_dollars,
                min_spread_dollars=settings.strategy_min_spread_dollars,
                probability_gap=settings.strategy_probability_gap,
                order_count=settings.strategy_order_count,
                limit_price_cents=settings.strategy_limit_price_cents,
                max_spread_dollars=settings.trade_max_entry_spread_dollars,
                entry_min_yes_ask_cents=settings.trade_entry_effective_min_yes_ask_cents,
            )
        else:
            intent = signal_from_bar_buy_no(
                ticker=ticker,
                no_bid_dollars=no_bid_d,
                no_ask_dollars=no_ask_d,
                max_yes_ask_dollars=settings.trade_entry_effective_max_yes_ask_dollars,
                min_spread_dollars=settings.strategy_min_spread_dollars,
                probability_gap=settings.strategy_probability_gap,
                order_count=settings.strategy_order_count,
                limit_price_cents=settings.strategy_limit_price_cents,
                max_spread_dollars=settings.trade_max_entry_spread_dollars,
                entry_min_yes_ask_cents=settings.trade_entry_effective_min_yes_ask_cents,
            )

    if intent is None:
        stats.no_rule_signal += 1
        return 0, stats

    if not execute:
        stats.skipped_cli_no_execute += 1
        log.warning(
            "bitcoin_trade_candidate",
            ticker=ticker,
            count=intent.count,
            yes_price_cents=intent.yes_price_cents,
            note="re-run with --execute and TRADE_BITCOIN_AUTO_EXECUTE=true",
        )
        return 0, stats

    if not settings.trade_bitcoin_auto_execute:
        stats.blocked_auto_execute_false += 1
        log.warning("bitcoin_trade_blocked", ticker=ticker, reason="TRADE_BITCOIN_AUTO_EXECUTE_false")
        return 0, stats

    trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
    stats.submitted += 1
    return stats.submitted, stats


def run_bitcoin_trade_pass(
    settings: Settings,
    *,
    execute: bool,
    log: StructuredLogger | None = None,
    rotation_counter: list[int] | None = None,
) -> tuple[int, BitcoinTradeRunStats]:
    """Fetch BTC spot + Kalshi book; one pass over pinned ticker or next contract in rotating prefix universe."""
    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)

    if not bitcoin_markets_configured(settings):
        raise ValueError(
            "Set TRADE_BITCOIN_KALSHI_TICKER to pin one contract, or leave it empty and set "
            "TRADE_CRYPTO_KALSHI_PREFIXES (e.g. KXBTC,KXETH) or TRADE_BITCOIN_TICKER_PREFIX (default discovery KXBTC)."
        )

    client = build_sdk_client(settings)
    risk = RiskManager(settings)
    ledger = DryRunLedger()
    rot = rotation_counter if rotation_counter is not None else [0]
    candidates = resolve_bitcoin_candidate_tickers(client, settings)
    if not candidates:
        raise ValueError(
            f"No open Kalshi markets matched prefixes {crypto_kalshi_prefixes_for_discovery(settings)!r}. "
            "Pin TRADE_BITCOIN_KALSHI_TICKER or widen TRADE_BITCOIN_DISCOVERY_MAX_PAGES / check Kalshi UI."
        )
    chosen = pick_next_bitcoin_ticker(candidates, rot)
    if chosen is None:
        raise ValueError("Bitcoin ticker rotation failed (empty candidate list).")
    return evaluate_bitcoin_ticker_pass(
        settings,
        client=client,
        risk=risk,
        ledger=ledger,
        log=log,
        execute=execute,
        market_ticker=chosen,
    )
