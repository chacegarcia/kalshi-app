"""Secondary crypto scanner: finds fee-edge YES entries, persists signals for ``llm-trade``, optional crypto-only exits."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kalshi_bot.bet_history import bet_history_extra_min_edge, should_skip_ticker_for_bet_history
from kalshi_bot.bitcoin_runner import crypto_kalshi_prefixes_for_discovery
from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings, project_root
from kalshi_bot.edge_math import implied_yes_ask_dollars, min_edge_threshold_for_mid, net_edge_buy_yes_long
from kalshi_bot.logger import StructuredLogger, get_logger
from kalshi_bot.market_data import (
    MarketSummary,
    fetch_open_markets_by_ticker_prefixes,
    get_orderbook,
    yes_bid_and_no_bid_cents_for_trading,
)
from kalshi_bot.monitor import record_event
from kalshi_bot.strategy import (
    entry_filter_timing_and_event,
    should_skip_buy_ticker_substrings,
    skip_buy_yes_longshot,
)
from kalshi_bot.trading import build_sdk_client


@dataclass
class CryptoOpportunity:
    ticker: str
    title: str
    net_edge: float
    mid_yes_dollars: float
    yes_ask_cents: int
    spread_dollars: float
    volume: int | None
    detail: str

    def to_json_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def crypto_watch_state_path(settings: Settings) -> Path:
    raw = (getattr(settings, "crypto_watch_state_path", None) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return project_root() / ".kalshi_crypto_watch.json"


def _effective_min_edge(settings: Settings) -> float:
    v = getattr(settings, "crypto_watch_min_net_edge_after_fees", None)
    if v is not None:
        return float(v)
    return float(settings.trade_min_net_edge_after_fees)


def _effective_mid_extra(settings: Settings) -> float:
    return float(settings.trade_edge_middle_extra_edge)


def evaluate_crypto_yes_opportunity(
    client: KalshiSdkClient,
    settings: Settings,
    summary: MarketSummary,
    event_data_cache: dict[str, list[tuple[str, float]] | None],
    *,
    log: StructuredLogger,
) -> CryptoOpportunity | None:
    ticker = summary.ticker
    title = (summary.title or "").strip() or ticker
    if should_skip_buy_ticker_substrings(settings, ticker):
        return None
    if should_skip_ticker_for_bet_history(ticker, settings):
        return None
    vol = summary.volume
    if settings.trade_min_market_volume is not None:
        if vol is None or vol < settings.trade_min_market_volume:
            return None
    try:
        ob = get_orderbook(client, ticker)
    except Exception as exc:  # noqa: BLE001
        log.debug("crypto_watch_skip_orderbook", ticker=ticker, error=str(exc))
        return None
    yb_c, nb_c = yes_bid_and_no_bid_cents_for_trading(ob)
    if nb_c is None or yb_c is None:
        return None
    yes_bid_d = yb_c / 100.0
    yes_ask_d = implied_yes_ask_dollars(nb_c / 100.0)
    yes_ask_c = int(max(1, min(99, round(yes_ask_d * 100.0))))
    spread = max(0.0, yes_ask_d - yes_bid_d)
    if spread < float(settings.strategy_min_spread_dollars):
        return None
    if settings.trade_max_entry_spread_dollars is not None and spread > float(
        settings.trade_max_entry_spread_dollars
    ):
        return None
    if yes_ask_d > float(settings.trade_entry_effective_max_yes_ask_dollars):
        return None
    mid = (yes_bid_d + yes_ask_d) / 2.0
    if skip_buy_yes_longshot(settings, yes_ask_c):
        return None
    skip_te, te_reason = entry_filter_timing_and_event(
        settings, client, ticker, yes_ask_c, event_data_cache
    )
    if skip_te:
        log.debug("crypto_watch_skip_timing", ticker=ticker, reason=te_reason)
        return None
    mn = _effective_min_edge(settings) + bet_history_extra_min_edge(ticker, settings)
    me = _effective_mid_extra(settings)
    edge = net_edge_buy_yes_long(fair_yes=mid, yes_ask_dollars=yes_ask_d, contracts=1)
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
        volume=int(vol) if vol is not None else None,
        detail="mid-as-fair YES edge vs ask",
    )


def scan_crypto_opportunities(
    client: KalshiSdkClient,
    settings: Settings,
    *,
    log: StructuredLogger | None = None,
) -> list[CryptoOpportunity]:
    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    prefixes = crypto_kalshi_prefixes_for_discovery(settings)
    if not prefixes:
        return []
    max_m = max(1, int(getattr(settings, "crypto_watch_max_markets_scan", 500)))
    max_pages = max(1, int(getattr(settings, "crypto_watch_max_pages", 80)))
    rows_m = fetch_open_markets_by_ticker_prefixes(
        client,
        prefixes=prefixes,
        max_results=max_m,
        max_api_pages=max_pages,
        mve_filter="exclude",
    )
    event_cache: dict[str, list[tuple[str, float]] | None] = {}
    out: list[CryptoOpportunity] = []
    for s in rows_m:
        op = evaluate_crypto_yes_opportunity(client, settings, s, event_cache, log=log)
        if op is not None:
            out.append(op)
    out.sort(key=lambda x: x.net_edge, reverse=True)
    return out


def build_crypto_watch_payload(
    opportunities: list[CryptoOpportunity],
    *,
    prefixes: list[str],
) -> dict[str, Any]:
    return {
        "updated_unix": time.time(),
        "prefixes": prefixes,
        "opportunities": [o.to_json_dict() for o in opportunities],
    }


def write_crypto_watch_state_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, indent=2, default=str)
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def post_crypto_watch_to_dashboard(settings: Settings, payload: dict[str, Any]) -> None:
    if not getattr(settings, "dashboard_ingest_crypto_watch", True):
        return
    port = int(settings.dashboard_port)
    body = json.dumps(payload, default=str).encode("utf-8")
    url = f"http://127.0.0.1:{port}/api/ingest_crypto_watch"
    try:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            if resp.status == 200:
                return
    except urllib.error.HTTPError:
        pass
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


def merge_crypto_watch_into_llm_rows(
    settings: Settings,
    rows: list[tuple[str, str]],
    open_volumes: dict[str, int | None],
) -> int:
    """Prepend priority tickers from crypto-watch and/or WebSocket ticker-scan JSON (merged, deduped by ticker, max net_edge)."""
    merge_cw = getattr(settings, "trade_llm_merge_crypto_watch_signals", True)
    merge_ws = getattr(settings, "trade_llm_merge_ws_ticker_scan_signals", True)
    if not merge_cw and not merge_ws:
        return 0

    paths: list[Path] = []
    if merge_cw:
        paths.append(crypto_watch_state_path(settings))
    if merge_ws:
        from kalshi_bot.ws_ticker_scan import ws_scan_state_path

        paths.append(ws_scan_state_path(settings))

    merged_items: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        opps = raw.get("opportunities")
        if not isinstance(opps, list):
            continue
        for item in opps:
            if isinstance(item, dict) and str(item.get("ticker") or "").strip():
                merged_items.append(item)

    best: dict[str, dict[str, Any]] = {}
    for item in merged_items:
        t = str(item.get("ticker") or "").strip()
        try:
            ne = float(item.get("net_edge") or -1e9)
        except (TypeError, ValueError):
            ne = -1e9
        prev = best.get(t)
        if prev is None:
            best[t] = item
            continue
        try:
            pne = float(prev.get("net_edge") or -1e9)
        except (TypeError, ValueError):
            pne = -1e9
        if ne > pne:
            best[t] = item

    opps = sorted(best.values(), key=lambda x: float(x.get("net_edge") or 0.0), reverse=True)
    if not opps:
        return 0
    max_prepend = max(0, int(getattr(settings, "trade_llm_crypto_watch_merge_max", 40)))
    if max_prepend == 0:
        return 0
    seen = {t for t, _ in rows}
    prepend: list[tuple[str, str]] = []
    for item in opps:
        t = str(item.get("ticker") or "").strip()
        if not t or t in seen:
            continue
        ti = str(item.get("title") or t).strip() or t
        vol = item.get("volume")
        vol_i: int | None = None
        if vol is not None:
            try:
                vol_i = int(vol)
            except (TypeError, ValueError):
                vol_i = None
        prepend.append((t, ti))
        if vol_i is not None:
            open_volumes[t] = vol_i
        seen.add(t)
        if len(prepend) >= max_prepend:
            break
    if not prepend:
        return 0
    rows[:] = prepend + rows
    return len(prepend)


def run_crypto_watch_iteration(
    settings: Settings,
    *,
    client: KalshiSdkClient | None = None,
    log: StructuredLogger | None = None,
    emit_dashboard_event: bool = True,
) -> tuple[list[CryptoOpportunity], dict[str, Any]]:
    """One scan: opportunities + payload written to disk and optionally POSTed to dashboard."""
    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    client = client or build_sdk_client(settings)
    opps = scan_crypto_opportunities(client, settings, log=log)
    prefixes = crypto_kalshi_prefixes_for_discovery(settings)
    payload = build_crypto_watch_payload(opps, prefixes=prefixes)
    path = crypto_watch_state_path(settings)
    write_crypto_watch_state_file(path, payload)
    post_crypto_watch_to_dashboard(settings, payload)
    if emit_dashboard_event and opps:
        top = opps[0]
        record_event(
            "crypto_watch_ping",
            market_title=f"{len(opps)} crypto edge candidates (top {top.ticker}, Δedge {top.net_edge:.4f})",
            tickers=[o.ticker for o in opps[:12]],
            top_ticker=top.ticker,
            top_net_edge=top.net_edge,
            count=len(opps),
            note="See .kalshi_crypto_watch.json or GET /api/crypto_watch; llm-trade merges when TRADE_LLM_MERGE_CRYPTO_WATCH_SIGNALS=true.",
        )
    log.info(
        "crypto_watch_iteration",
        opportunities=len(opps),
        path=str(path),
        prefixes=prefixes,
    )
    return opps, payload


def run_crypto_watch_loop(
    settings: Settings,
    *,
    interval_seconds: float,
    run_crypto_exits: bool,
    exit_execute: bool,
    log: StructuredLogger | None = None,
) -> None:
    """Continuous crypto opportunity scan + optional TP/SL on crypto long YES only."""
    from kalshi_bot.auto_sell import auto_sell_scan_all_long_yes, collect_exit_scan_rows, format_exit_scan_summary

    log = log or get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    client = build_sdk_client(settings)

    def _is_crypto(t: str) -> bool:
        pfx = [p.upper() for p in crypto_kalshi_prefixes_for_discovery(settings)]
        u = t.upper()
        return any(u.startswith(p) for p in pfx)

    iv = max(5.0, float(interval_seconds))
    print(
        f"crypto-watch: interval {iv:.0f}s — opportunities → {crypto_watch_state_path(settings)} "
        f"+ dashboard /api/ingest_crypto_watch. "
        f"Exits={'on' if run_crypto_exits else 'off'} "
        f"({'submit' if exit_execute else 'read-only summary'}).",
        flush=True,
    )
    iteration = 0
    try:
        while True:
            iteration += 1
            print(f"--- crypto-watch iteration {iteration} ---", flush=True)
            opps, _ = run_crypto_watch_iteration(
                settings, client=client, log=log, emit_dashboard_event=True
            )
            print(f"  opportunities (fee-edge YES vs mid): {len(opps)}", flush=True)
            for o in opps[:15]:
                print(
                    f"    {o.ticker}  edge={o.net_edge:.4f}  mid≈{o.mid_yes_dollars:.3f}  ask≈{o.yes_ask_cents}¢  {o.title[:64]}",
                    flush=True,
                )
            if len(opps) > 15:
                print(f"    … +{len(opps) - 15} more", flush=True)

            if run_crypto_exits:
                if exit_execute:
                    n, lines = auto_sell_scan_all_long_yes(
                        client,
                        settings,
                        cli_min_yes_bid_cents=None,
                        log=log,
                        ticker_filter=_is_crypto,
                    )
                    if lines:
                        print("--- crypto-watch exits (crypto positions only) ---", flush=True)
                        for line in lines:
                            print(f"  {line}", flush=True)
                        print(f"  sells submitted: {n}", flush=True)
                    else:
                        print("  crypto exits: no sells this pass.", flush=True)
                else:
                    rows = collect_exit_scan_rows(client, settings, cli_min_yes_bid_cents=None, log=log)
                    crypto_rows = [r for r in rows if _is_crypto(r.ticker)]
                    print("--- crypto-watch exit scan (crypto positions only, read-only) ---", flush=True)
                    for line in format_exit_scan_summary(crypto_rows):
                        print(line, flush=True)

            print(f"Sleeping {iv:.0f}s…\n", flush=True)
            time.sleep(iv)
    except KeyboardInterrupt:
        print("\ncrypto-watch stopped.", flush=True)
