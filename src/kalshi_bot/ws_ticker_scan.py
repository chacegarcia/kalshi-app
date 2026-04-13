"""WebSocket ticker stream scanner: deterministic fee-edge candidates without LLM.

Uses ``KalshiWS`` to subscribe to the global ``ticker`` channel and **debounce** by market. By default
(``TRADE_WS_SCAN_USE_REST_ORDERBOOK=true``) each candidate reuses ``evaluate_crypto_yes_opportunity`` — same REST
orderbook + implied YES ask + your ``.env`` gates as ``crypto-watch``. Raw WS ``yes_bid``/``yes_ask`` alone are a
**poor** fair value (mid−ask is typically negative); ticker-only mode is legacy.

Writes ``.kalshi_ws_ticker_scan.json`` for ``llm-trade`` to prepend when
``TRADE_LLM_MERGE_WS_TICKER_SCAN_SIGNALS=true`` (see ``merge_crypto_watch_into_llm_rows``).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from kalshi_bot.auth import build_kalshi_auth
from kalshi_bot.bet_history import bet_history_extra_min_edge, should_skip_ticker_for_bet_history
from kalshi_bot.bitcoin_runner import crypto_kalshi_prefixes_for_discovery
from kalshi_bot.config import Settings, project_root
from kalshi_bot.crypto_watch import (
    CryptoOpportunity,
    build_crypto_watch_payload,
    evaluate_crypto_yes_opportunity,
    post_crypto_watch_to_dashboard,
    write_crypto_watch_state_file,
    _effective_mid_extra,
    _effective_min_edge,
)
from kalshi_bot.market_data import MarketSummary
from kalshi_bot.edge_math import min_edge_threshold_for_mid, net_edge_buy_yes_long
from kalshi_bot.logger import StructuredLogger, get_logger
from kalshi_bot.monitor import record_event
from kalshi_bot.strategy import should_skip_buy_ticker_substrings, skip_buy_yes_longshot
from kalshi_bot.trading import build_sdk_client
from kalshi_bot.ws import KalshiWS


def ws_scan_state_path(settings: Settings) -> Path:
    raw = (settings.trade_ws_scan_state_path or "").strip()
    if raw:
        return Path(raw).expanduser()
    return project_root() / ".kalshi_ws_ticker_scan.json"


def _parse_dollar_field(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val))
    except ValueError:
        return None


def parse_kalshi_ticker_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Return ``market_ticker``, ``yes_bid_dollars``, ``yes_ask_dollars``, optional title/volume, or None."""
    if msg.get("type") != "ticker":
        return None
    body = msg.get("msg") or {}
    ticker = body.get("market_ticker") or body.get("ticker")
    if not ticker:
        return None
    bid = _parse_dollar_field(body.get("yes_bid_dollars"))
    ask = _parse_dollar_field(body.get("yes_ask_dollars"))
    if bid is None or ask is None:
        return None
    title = str(body.get("market_title") or body.get("title") or ticker).strip() or str(ticker)
    vol = body.get("volume")
    vol_i: int | None = None
    if vol is not None:
        try:
            vol_i = int(vol)
        except (TypeError, ValueError):
            vol_i = None
    return {
        "ticker": str(ticker),
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "title": title,
        "volume": vol_i,
    }


def _ticker_matches_ws_prefixes(settings: Settings, ticker: str) -> bool:
    raw = (settings.trade_ws_scan_ticker_prefixes or "").strip()
    if raw:
        pfx = [p.strip().upper() for p in raw.split(",") if p.strip()]
    else:
        pfx = [p.upper() for p in crypto_kalshi_prefixes_for_discovery(settings)]
    if not pfx:
        return True
    u = ticker.upper()
    return any(u.startswith(p) for p in pfx)


def evaluate_ws_ticker_opportunity(
    settings: Settings,
    *,
    ticker: str,
    title: str,
    yes_bid_dollars: float,
    yes_ask_dollars: float,
    volume: int | None,
) -> CryptoOpportunity | None:
    """Ticker-only edge (legacy): uses WS best YES bid/ask. Usually ``mid − ask`` is negative — prefer REST orderbook."""
    if not _ticker_matches_ws_prefixes(settings, ticker):
        return None
    if should_skip_buy_ticker_substrings(settings, ticker):
        return None
    if settings.trade_ws_scan_respect_min_volume and settings.trade_min_market_volume is not None:
        if volume is None or volume < settings.trade_min_market_volume:
            return None

    spread = max(0.0, yes_ask_dollars - yes_bid_dollars)
    if spread < float(settings.strategy_min_spread_dollars):
        return None
    if settings.trade_max_entry_spread_dollars is not None and spread > float(
        settings.trade_max_entry_spread_dollars
    ):
        return None
    if yes_ask_dollars > float(settings.trade_entry_effective_max_yes_ask_dollars):
        return None

    yes_ask_c = int(max(1, min(99, round(yes_ask_dollars * 100.0))))
    mid = (yes_bid_dollars + yes_ask_dollars) / 2.0
    if skip_buy_yes_longshot(settings, yes_ask_c):
        return None

    mn = _effective_min_edge(settings) + bet_history_extra_min_edge(ticker, settings)
    me = _effective_mid_extra(settings)
    edge = net_edge_buy_yes_long(fair_yes=mid, yes_ask_dollars=yes_ask_dollars, contracts=1)
    need = min_edge_threshold_for_mid(mid, base_min_edge=mn, middle_extra=me)
    if edge < need:
        return None

    return CryptoOpportunity(
        ticker=ticker,
        title=title,
        net_edge=float(edge),
        mid_yes_dollars=float(mid),
        yes_ask_cents=yes_ask_c,
        spread_dollars=float(spread),
        volume=volume,
        detail="ws ticker: mid-as-fair YES edge vs ask (no LLM)",
    )


def run_ws_ticker_scan(
    settings: Settings,
    *,
    log: StructuredLogger | None = None,
) -> None:
    """Run forever: Kalshi ``ticker`` WebSocket → edge filter → write signal JSON + optional dashboard ping."""
    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    client = build_sdk_client(settings) if settings.trade_ws_scan_use_rest_orderbook else None
    event_data_cache: dict[str, list[tuple[str, float]] | None] = {}
    auth = build_kalshi_auth(
        settings.kalshi_api_key_id,
        key_path=settings.kalshi_private_key_path,
        key_pem=settings.kalshi_private_key_pem,
    )

    best_by_ticker: dict[str, CryptoOpportunity] = {}
    last_eval: dict[str, float] = {}
    min_gap = max(0.0, float(settings.trade_ws_scan_min_seconds_between_same_ticker))
    flush_every = max(0.5, float(settings.trade_ws_scan_flush_seconds))
    last_flush = 0.0
    loop_started = time.monotonic()
    stats: dict[str, int] = {
        "raw_msgs": 0,
        "ticker_parsed": 0,
        "edge_passed": 0,
    }

    def flush_payload() -> None:
        nonlocal last_flush
        opps = sorted(best_by_ticker.values(), key=lambda o: o.net_edge, reverse=True)
        max_n = max(1, int(settings.trade_ws_scan_max_opportunities_in_file))
        opps = opps[:max_n]
        prefixes = (
            [p.strip() for p in (settings.trade_ws_scan_ticker_prefixes or "").split(",") if p.strip()]
            if (settings.trade_ws_scan_ticker_prefixes or "").strip()
            else crypto_kalshi_prefixes_for_discovery(settings)
        )
        payload = build_crypto_watch_payload(opps, prefixes=prefixes)
        payload["source"] = "ws_ticker_scan"
        path = ws_scan_state_path(settings)
        write_crypto_watch_state_file(path, payload)
        post_crypto_watch_to_dashboard(settings, payload)
        if opps and settings.trade_ws_scan_emit_dashboard_ping:
            top = opps[0]
            record_event(
                "crypto_watch_ping",
                market_title=f"WS ticker scan: {len(opps)} edge candidates (top {top.ticker})",
                tickers=[o.ticker for o in opps[:12]],
                top_ticker=top.ticker,
                top_net_edge=top.net_edge,
                count=len(opps),
                note="From WebSocket ticker channel — see .kalshi_ws_ticker_scan.json",
            )
        log.info("ws_ticker_scan_flush", path=str(path), opportunities=len(opps))
        last_flush = time.time()

    async def on_message(msg: dict[str, Any]) -> None:
        nonlocal last_flush
        stats["raw_msgs"] += 1
        mtype = msg.get("type")
        if mtype == "error":
            print(f"  Kalshi WebSocket error frame: {msg}", flush=True)
            return
        parsed = parse_kalshi_ticker_message(msg)
        if parsed is None:
            return
        stats["ticker_parsed"] += 1
        ticker = parsed["ticker"]
        if not _ticker_matches_ws_prefixes(settings, ticker):
            return
        now = time.time()
        if now - last_eval.get(ticker, 0.0) < min_gap:
            return
        last_eval[ticker] = now

        if settings.trade_ws_scan_use_rest_orderbook:
            if client is None:
                return
            summary = MarketSummary(
                ticker=ticker,
                title=parsed["title"],
                status=None,
                volume=parsed["volume"],
            )
            op = await asyncio.to_thread(
                evaluate_crypto_yes_opportunity,
                client,
                settings,
                summary,
                event_data_cache,
                log,
            )
        else:
            op = evaluate_ws_ticker_opportunity(
                settings,
                ticker=ticker,
                title=parsed["title"],
                yes_bid_dollars=parsed["yes_bid_dollars"],
                yes_ask_dollars=parsed["yes_ask_dollars"],
                volume=parsed["volume"],
            )
        if op is None:
            return
        stats["edge_passed"] += 1
        prev = best_by_ticker.get(ticker)
        if prev is None or op.net_edge > prev.net_edge:
            best_by_ticker[ticker] = op

        if now - last_flush >= flush_every:
            flush_payload()

    def _prefix_hint() -> str:
        raw = (settings.trade_ws_scan_ticker_prefixes or "").strip()
        if raw:
            return raw
        return ",".join(crypto_kalshi_prefixes_for_discovery(settings)) or "(all)"

    async def _heartbeat() -> None:
        interval = 15.0
        try:
            while True:
                await asyncio.sleep(interval)
                r, t, e = stats["raw_msgs"], stats["ticker_parsed"], stats["edge_passed"]
                up = (time.monotonic() - loop_started) / 60.0
                print(
                    f"  … ws-ticker-scan loop {up:.1f}m: {r} raw frames, {t} ticker w/ bid/ask, "
                    f"{e} edge hits (stderr = connect/reconnect).",
                    flush=True,
                )
        except asyncio.CancelledError:
            raise

    async def _on_connected() -> None:
        print(
            "  Connected — streaming ticker updates. "
            "If heartbeats show 0 raw frames, check KALSHI_ENV / KALSHI_WS_URL vs your API key.",
            flush=True,
        )

    async def _runner() -> None:
        print(
            f"ws-ticker-scan: Kalshi WebSocket `ticker` → {ws_scan_state_path(settings)} "
            f"(prefixes: {_prefix_hint()}). Runs in a continuous loop until Ctrl+C.\n",
            flush=True,
        )
        if settings.trade_ws_scan_use_rest_orderbook:
            print(
                "  Mode: REST orderbook — uses the same ``evaluate_crypto_yes_opportunity`` rules as "
                "``crypto-watch`` (your spread/edge/timing/.env gates). WS only picks which tickers to refresh.",
                flush=True,
            )
        else:
            print(
                "  Mode: ticker bid/ask only (no REST) — fee-edge vs mid is usually negative; set "
                "TRADE_WS_SCAN_USE_REST_ORDERBOOK=true for real signals.",
                flush=True,
            )
        print(
            "  Trade bot: ensure TRADE_LLM_MERGE_WS_TICKER_SCAN_SIGNALS=true, then run "
            "``kalshi-bot llm-trade`` — prepends tickers from .kalshi_ws_ticker_scan.json.",
            flush=True,
        )
        print(f"  Connecting to {settings.ws_url} … (connection log on stderr)", flush=True)
        hb = asyncio.create_task(_heartbeat())
        try:
            ws = KalshiWS(
                ws_url=settings.ws_url,
                auth=auth,
                on_message=on_message,
                on_connected=_on_connected,
            )
            await ws.run(market_tickers=[])
        finally:
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        try:
            flush_payload()
        except Exception:
            pass
        print("\nws-ticker-scan stopped.", flush=True)
