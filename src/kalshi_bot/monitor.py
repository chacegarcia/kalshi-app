"""Local web dashboard to watch orders and risk events while the bot runs."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings, project_root
from kalshi_bot.portfolio import fetch_portfolio_snapshot, list_resting_orders_detail

_LOG = logging.getLogger(__name__)
_LOCK = Lock()
_SELL_LOOP_STOP = threading.Event()
_SELL_LOOP_THREAD: threading.Thread | None = None
_SELL_LOOP_LOCK = Lock()
_EVENTS: deque[dict[str, Any]] = deque(maxlen=500)
# Kinds accepted by POST /api/ingest_event (cross-process feed); heartbeat is never forwarded.
_INGESTABLE_EVENT_KINDS = frozenset(
    {
        "dry_run",
        "live_submit",
        "live_ack",
        "blocked",
        "refused",
        "auto_sell_profit_estimate",
    }
)
# Last trade-pass summary (JSON-safe dict) for GET /api/pass_summary — no HTML panel.
_LAST_PASS_SUMMARY: dict[str, Any] = {}
# Time series for dashboard line chart (cash, positions MTM from portfolio_value, total = cash + positions)
_SERIES: deque[dict[str, Any]] = deque(maxlen=2000)
# Closed-trade tally (auto-sell: estimated gross vs entry, or take-profit without basis)
_WINS = 0
_LOSSES = 0
_TIES = 0  # breakeven (estimated gross == 0)


def record_trade_outcome(pnl_cents: float | None) -> None:
    """Increment dashboard W–L from signed estimated PnL (cents) on a close.

    Positive → win, negative → loss, zero → tie (breakeven). ``None`` → no-op (use
    ``record_auto_sell_outcome`` when entry was unknown).
    """
    global _WINS, _LOSSES, _TIES
    if pnl_cents is None:
        return
    try:
        p = float(pnl_cents)
    except (TypeError, ValueError):
        return
    with _LOCK:
        if p > 0:
            _WINS += 1
        elif p < 0:
            _LOSSES += 1
        else:
            _TIES += 1


def _apply_auto_sell_outcome_and_event(
    *,
    gross_profit_cents: int | None,
    exit_reason: str,
    event_payload: dict[str, Any],
) -> None:
    """Update W–L and append one dashboard row (in-process only)."""
    record_event("auto_sell_profit_estimate", **event_payload)
    record_auto_sell_outcome(gross_profit_cents=gross_profit_cents, exit_reason=exit_reason)


def notify_auto_sell_outcome(
    settings: Settings,
    *,
    gross_profit_cents: int | None,
    exit_reason: str,
    event_payload: dict[str, Any],
) -> None:
    """Update session W–L and the HTML feed for an auto-sell.

    When the dashboard runs in another process (e.g. ``kalshi-bot run`` / ``llm-trade`` with ``--web``),
    tries ``POST /api/ingest_auto_sell`` on ``127.0.0.1:{DASHBOARD_PORT}`` so counts and events stay in sync.
    If nothing is listening or ``DASHBOARD_INGEST_AUTO_SELL=false``, applies updates in-process only.
    """
    if not settings.dashboard_ingest_auto_sell:
        _apply_auto_sell_outcome_and_event(
            gross_profit_cents=gross_profit_cents,
            exit_reason=exit_reason,
            event_payload=event_payload,
        )
        return

    body = json.dumps(
        {
            "gross_profit_cents": gross_profit_cents,
            "exit_reason": exit_reason,
            "event_payload": event_payload,
        },
        default=str,
    ).encode("utf-8")
    url = f"http://127.0.0.1:{int(settings.dashboard_port)}/api/ingest_auto_sell"
    try:
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            if resp.status == 200:
                return
    except urllib.error.HTTPError:
        # Non-2xx from server — do not assume ingest ran; fall back to in-process.
        pass
    except (urllib.error.URLError, TimeoutError, OSError):
        pass

    _apply_auto_sell_outcome_and_event(
        gross_profit_cents=gross_profit_cents,
        exit_reason=exit_reason,
        event_payload=event_payload,
    )


def _dashboard_sell_loop_worker() -> None:
    """Background exit-scan loop for the dashboard (same rules as ``kalshi-bot sell-bot``)."""
    from kalshi_bot.auto_sell import auto_sell_scan_all_long_yes
    from kalshi_bot.config import get_settings
    from kalshi_bot.logger import get_logger
    from kalshi_bot.trading import build_sdk_client

    settings = get_settings()
    interval = max(5.0, float(settings.sell_bot_interval_seconds))
    log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
    client = build_sdk_client(settings)
    while not _SELL_LOOP_STOP.is_set():
        try:
            n, _lines = auto_sell_scan_all_long_yes(client, settings, cli_min_yes_bid_cents=None, log=log)
            if n:
                _LOG.info("dashboard_sell_loop_submitted", orders=n)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("dashboard_sell_loop_iteration_failed", error=str(exc))
        if _SELL_LOOP_STOP.wait(timeout=interval):
            break


def notify_portfolio_series_to_dashboard(settings: Settings) -> None:
    """POST to the local dashboard so it appends one portfolio chart point (no --web trade process, or split process)."""
    if not getattr(settings, "dashboard_ingest_portfolio_series", True):
        return
    url = f"http://127.0.0.1:{int(settings.dashboard_port)}/api/ingest_portfolio_series"
    try:
        req = urllib.request.Request(
            url,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            if resp.status == 200:
                return
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        pass


def record_auto_sell_outcome(*, gross_profit_cents: int | None, exit_reason: str) -> None:
    """Update W–L after auto-sell submitted an exit (take-profit or stop-loss).

    When ``gross_profit_cents`` is set, sign decides W/L/BE. When ``None``, ``take_profit_*``
    counts as a win (basis unknown); ``stop_loss_*`` counts as a loss.
    """
    global _WINS, _LOSSES
    if gross_profit_cents is not None:
        record_trade_outcome(float(gross_profit_cents))
        return
    if exit_reason.startswith("take_profit"):
        with _LOCK:
            _WINS += 1
        return
    if exit_reason.startswith("trailing_stop") or exit_reason.startswith("profit_lock"):
        with _LOCK:
            _TIES += 1
        return
    if exit_reason.startswith("stop_loss"):
        with _LOCK:
            _LOSSES += 1


def win_loss_snapshot() -> dict[str, int]:
    """Return current win/loss/tie counts (thread-safe copy)."""
    with _LOCK:
        return {"wins": _WINS, "losses": _LOSSES, "ties": _TIES}


def structured_log_path_for_dashboard() -> Path:
    """Path to structured JSONL for GET /api/log_summary (``STRUCTURED_LOG_PATH`` or repo default)."""
    raw = os.environ.get("STRUCTURED_LOG_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return project_root() / "logs" / "kalshi_bot.jsonl"


def record_trade_pass_summary(
    *,
    command: str,
    iteration: int,
    orders_submitted: int,
    stats: dict[str, Any],
) -> None:
    """Store last pipeline pass stats for ``GET /api/pass_summary`` (API/background only; no HTML card)."""
    global _LAST_PASS_SUMMARY
    row: dict[str, Any] = {
        "updated_unix": time.time(),
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "iteration": iteration,
        "orders_submitted": orders_submitted,
        "stats": stats,
    }
    with _LOCK:
        _LAST_PASS_SUMMARY = row
    _LOG.debug(
        "trade_pass_summary_recorded",
        extra={"command": command, "iteration": iteration, "orders_submitted": orders_submitted},
    )


def record_portfolio_series_point(
    balance_cents: int | float | None,
    positions_value_cents: int | float | None,
    *,
    exposure_sum_cents: float | None = None,
) -> None:
    """Append one point for the live line chart (thread-safe).

    ``positions_value_cents`` should be Kalshi's balance API ``portfolio_value`` (mark-to-market positions),
    which matches the mobile app. If omitted, falls back to ``exposure_sum_cents`` (sum of per-market
    ``market_exposure_dollars``). ``total_account_cents`` is cash plus rounded positions value when cash is known.
    """
    balance_known = balance_cents is not None
    bal = 0
    if balance_known:
        try:
            bal = int(balance_cents)
        except (TypeError, ValueError):
            bal = 0
            balance_known = False

    if positions_value_cents is not None:
        display_pv = float(positions_value_cents)
    elif exposure_sum_cents is not None:
        display_pv = float(exposure_sum_cents)
    else:
        display_pv = 0.0

    total_cents: int | None = None
    if balance_known:
        total_cents = bal + int(round(display_pv))
    row: dict[str, Any] = {
        "unix": time.time(),
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "balance_cents": bal,
        "balance_known": balance_known,
        "positions_value_cents": float(positions_value_cents)
        if positions_value_cents is not None
        else None,
        "exposure_sum_cents": float(exposure_sum_cents) if exposure_sum_cents is not None else None,
        "exposure_cents": display_pv,
    }
    if total_cents is not None:
        row["total_account_cents"] = total_cents
    with _LOCK:
        _SERIES.append(row)


def _stop_floor_cents(entry_cents: int, fraction: float) -> int:
    """Same as auto-sell: best YES bid at/below this triggers fixed stop (when enabled)."""
    return max(1, min(99, int(round(entry_cents * fraction))))


def dashboard_position_exit_hints(
    settings: Settings,
    *,
    entry_cents: int | None,
    entry_source: str,
    best_bid_cents: int | None,
) -> dict[str, Any]:
    """P/L vs entry + next TP bid / stop floor for sidebar (mirrors main TRADE_EXIT_* rules, simplified)."""
    out: dict[str, Any] = {
        "pnl_sign": "unknown",
        "unrealized_delta_cents": None,
        "take_profit_levels_cents": [],
        "take_profit_next_bid_cents": None,
        "cents_to_take_profit": None,
        "stop_loss_bid_cents": None,
        "cents_buffer_to_stop": None,
        "stop_loss_status": "off",
    }
    if entry_cents is None or not (1 <= entry_cents <= 99):
        out["stop_loss_status"] = "no_entry"
        return out
    if best_bid_cents is None:
        return out
    bid = int(best_bid_cents)
    ent = int(entry_cents)
    delta = bid - ent
    out["unrealized_delta_cents"] = delta
    if delta > 0:
        out["pnl_sign"] = "positive"
    elif delta < 0:
        out["pnl_sign"] = "negative"
    else:
        out["pnl_sign"] = "neutral"

    tp_levels: list[int] = []
    mult = float(settings.trade_exit_take_profit_min_bid_vs_entry_multiplier)
    if mult > 1.0:
        lv = int(math.ceil(float(ent) * mult - 1e-9))
        tp_levels.append(max(1, min(99, lv)))
    mpc = settings.trade_exit_min_profit_cents_for_entry(ent)
    if mpc is not None:
        lv = int(math.ceil(float(ent) + float(mpc) - 1e-9))
        tp_levels.append(max(1, min(99, lv)))
    if not settings.trade_exit_only_profit_margin:
        t_min = settings.auto_sell_effective_min_yes_bid_cents(None)
        if t_min is not None:
            tp_levels.append(max(1, min(99, int(t_min))))

    if tp_levels:
        tp_sorted = sorted(set(tp_levels))
        out["take_profit_levels_cents"] = tp_sorted
        next_above: int | None = None
        for t in tp_sorted:
            if bid < t:
                next_above = t
                break
        out["take_profit_next_bid_cents"] = next_above
        out["cents_to_take_profit"] = (next_above - bid) if next_above is not None else 0

    if settings.trade_exit_stop_loss_enabled:
        skip = (
            settings.trade_exit_stop_loss_skip_suspect_portfolio_estimate
            and entry_source == "portfolio"
            and (ent >= 95 or ent <= 5)
        )
        if skip:
            out["stop_loss_status"] = "skipped_suspect"
        else:
            frac = float(settings.trade_exit_stop_loss_entry_fraction)
            stop_bid = _stop_floor_cents(ent, frac)
            out["stop_loss_bid_cents"] = stop_bid
            out["cents_buffer_to_stop"] = bid - stop_bid
            out["stop_loss_status"] = "active"

    return out


def _json_safe(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    return str(obj)


def _append_event_row(row: dict[str, Any]) -> None:
    with _LOCK:
        _EVENTS.appendleft(row)


def record_event(kind: str, **payload: Any) -> None:
    """Append one row for the dashboard (thread-safe).

    When ``DASHBOARD_INGEST_TRADE_EVENTS`` is true (default), tries ``POST /api/ingest_event`` on the local
    dashboard port first so the Trades & orders feed updates if trading runs in another process. On HTTP 200,
    skips in-process append in this process (the dashboard process stores the row). Heartbeats are always local.
    """
    safe = {k: _json_safe(v) for k, v in payload.items()}
    row = {
        "kind": kind,
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "unix": time.time(),
        **safe,
    }
    if kind == "heartbeat":
        _append_event_row(row)
        return

    from kalshi_bot.config import get_settings

    settings = get_settings()
    if getattr(settings, "dashboard_ingest_trade_events", True):
        body = json.dumps(row, default=str).encode("utf-8")
        url = f"http://127.0.0.1:{int(settings.dashboard_port)}/api/ingest_event"
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                if resp.status == 200:
                    return
        except urllib.error.HTTPError:
            pass
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    _append_event_row(row)


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Kalshi bot monitor</title>
  <style>
    :root { --bg:#0f1419; --card:#1a2332; --text:#e7ecf3; --muted:#8b9aab; --ok:#3ecf8e; --warn:#f5a623; --err:#f25151; }
    * { box-sizing: border-box; }
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--text); margin:0; padding:0; display: flex; min-height: 100vh; align-items: stretch; overflow-x: hidden; }
    .main-wrap { flex: 1; padding: 1rem 1.25rem 2rem; min-width: 0; max-width: 100%; overflow-x: hidden; }
    .sidebar {
      width: 320px; flex-shrink: 0; background: #151b26; border-right: 1px solid #2a3544;
      display: flex; flex-direction: column; transition: margin-left 0.2s ease, width 0.2s ease;
    }
    .sidebar.collapsed { width: 0; margin-left: -320px; overflow: hidden; border: none; }
    .sidebar__head { display: flex; align-items: center; gap: 0.5rem; padding: 0.75rem 1rem; border-bottom: 1px solid #2a3544; }
    .sidebar__head button { background: #243044; border: none; color: var(--text); padding: 0.35rem 0.55rem; border-radius: 4px; cursor: pointer; font-size: 1rem; }
    .sidebar__head button:hover { background: #2d3d52; }
    .sidebar__body { padding: 0.75rem 1rem 1.25rem; overflow: auto; flex: 1; }
    .sidebar__hint { font-size: 0.75rem; color: var(--muted); margin: 0 0 0.75rem; line-height: 1.4; }
    .sidebar__hint code { font-size: 0.7rem; }
    .sidebar label { display: block; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-bottom: 0.35rem; }
    .seg { display: flex; flex-wrap: wrap; gap: 0.35rem; margin-bottom: 0.75rem; }
    .seg button {
      flex: 1; min-width: 3.2rem; padding: 0.45rem 0.35rem; border-radius: 6px; border: 1px solid #2a3544;
      background: #1e2838; color: var(--text); cursor: pointer; font-weight: 600; font-size: 0.85rem;
    }
    .seg button:hover { background: #243044; }
    .seg button.active { border-color: #6eb5ff; background: rgba(110,181,255,0.15); color: #b8d9ff; }
    .sidebar__stat { font-size: 0.8rem; margin: 0 0 1rem; color: var(--muted); }
    .sidebar__pre { font-size: 0.65rem; color: #a8b4c5; background: #0b0e14; padding: 0.5rem; border-radius: 6px; max-height: 10rem; overflow: auto; white-space: pre-wrap; word-break: break-word; margin: 0; }
    .open-pos { margin-top: 0.75rem; padding-top: 0.75rem; border-top: 1px solid #2a3544; }
    .open-pos h3 { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 0 0 0.45rem; font-weight: 600; }
    .mini-pos { border-radius: 8px; border: 1px solid #2a3544; background: #0b0e14; padding: 0.5rem 0.55rem; margin-bottom: 0.45rem; font-size: 0.78rem; }
    .mini-pos--pos {
      border-color: rgba(62,207,142,0.42);
      background: linear-gradient(160deg, rgba(62,207,142,0.12) 0%, #0b0e14 55%);
    }
    .mini-pos--neg {
      border-color: rgba(242,81,81,0.42);
      background: linear-gradient(160deg, rgba(242,81,81,0.1) 0%, #0b0e14 55%);
    }
    .mini-pos--neutral { border-color: #3a4555; }
    .mini-pos__pl { font-size: 0.72rem; font-weight: 600; font-variant-numeric: tabular-nums; margin-bottom: 0.25rem; }
    .mini-pos__pl--pos { color: #5ee4a8; }
    .mini-pos__pl--neg { color: #ff8a8a; }
    .mini-pos__pl--zero { color: var(--muted); }
    .mini-pos__exit { font-size: 0.68rem; color: #8b9aab; line-height: 1.4; margin: 0.35rem 0 0.2rem; }
    .mini-pos__exit span.tp { color: #6ecf9a; }
    .mini-pos__exit span.sl { color: #f09090; }
    .mini-pos__title { font-size: 0.72rem; color: var(--text); line-height: 1.3; margin-bottom: 0.3rem; word-break: break-word; }
    .mini-pos__shares { font-size: 0.74rem; font-weight: 600; font-variant-numeric: tabular-nums; color: #d0dae8; margin-bottom: 0.25rem; }
    .mini-pos__ticker { font-family: ui-monospace, monospace; font-size: 0.65rem; color: #8b9aab; margin-bottom: 0.25rem; }
    .mini-pos__row { display: flex; justify-content: space-between; gap: 0.5rem; color: #a8b4c5; font-variant-numeric: tabular-nums; font-size: 0.72rem; }
    .mini-pos__actions { display: flex; gap: 0.35rem; margin-top: 0.4rem; }
    .mini-pos__actions button {
      flex: 1; font-size: 0.7rem; padding: 0.32rem 0.2rem; border-radius: 5px; border: 1px solid #2a3544;
      background: #1e2838; color: var(--text); cursor: pointer; font-weight: 600;
    }
    .mini-pos__actions button:hover:not(:disabled) { background: #243044; }
    .mini-pos__actions button.dd { border-color: rgba(110,181,255,0.35); }
    .mini-pos__actions button.sell { border-color: rgba(245,166,35,0.4); }
    .mini-pos__actions button:disabled { opacity: 0.45; cursor: not-allowed; }
    .sell-loop-top { margin-bottom: 1rem; padding-bottom: 0.85rem; border-bottom: 1px solid #2a3544; }
    .sell-loop-top h3 { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 0 0 0.45rem; font-weight: 600; }
    .sell-loop-bar__hint { margin: 0.5rem 0 0 !important; font-size: 0.72rem !important; line-height: 1.4 !important; }
    .sell-loop-pill {
      display: flex; align-items: center; justify-content: space-between; gap: 0.65rem;
      width: 100%; padding: 0.45rem 0.65rem 0.45rem 0.85rem; border-radius: 999px;
      border: 1px solid #2e3d52; background: #1a2332; color: var(--text);
      cursor: pointer; font: inherit; text-align: left;
      transition: border-color 0.15s ease, background 0.15s ease;
    }
    .sell-loop-pill__text { display: flex; flex-direction: column; gap: 0.12rem; min-width: 0; flex: 1; }
    .sell-loop-pill:hover { border-color: #3a4d62; background: #1e2838; }
    .sell-loop-pill.is-on {
      border-color: rgba(62,207,142,0.45);
      background: linear-gradient(145deg, rgba(62,207,142,0.14) 0%, #1a2332 70%);
    }
    .sell-loop-pill__title { font-size: 0.78rem; font-weight: 700; letter-spacing: 0.02em; }
    .sell-loop-pill__sub { font-size: 0.68rem; color: var(--muted); font-weight: 500; margin-top: 0.12rem; }
    .sell-loop-pill__track {
      flex-shrink: 0; width: 2.75rem; height: 1.45rem; border-radius: 999px;
      background: #2a3544; position: relative; transition: background 0.15s ease;
    }
    .sell-loop-pill.is-on .sell-loop-pill__track { background: rgba(62,207,142,0.35); }
    .sell-loop-pill__thumb {
      position: absolute; top: 0.15rem; left: 0.15rem; width: 1.15rem; height: 1.15rem; border-radius: 50%;
      background: #8b9aab; transition: transform 0.18s ease, background 0.15s ease;
    }
    .sell-loop-pill.is-on .sell-loop-pill__thumb {
      transform: translateX(1.25rem);
      background: #3ecf8e;
    }
    #posStatus { font-size: 0.68rem; color: var(--muted); margin: 0 0 0.4rem; min-height: 1.1em; line-height: 1.35; }
    h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.5rem; }
    p.sub { color: var(--muted); font-size: 0.875rem; margin: 0 0 1rem; }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 0.75rem; color: #c5d0dc; }
    .status { display: inline-block; padding: 0.15rem 0.45rem; border-radius: 4px; background: var(--card); font-size: 0.75rem; color: var(--muted); margin-bottom: 0.75rem; }
    .balance-banner { display: flex; flex-wrap: wrap; gap: 0.5rem 1.25rem; align-items: baseline; background: var(--card); border-radius: 8px; padding: 0.65rem 1rem; margin-bottom: 0.75rem; font-size: 0.9375rem; }
    .balance-banner strong { color: var(--text); font-weight: 600; }
    .balance-banner span.muted { color: var(--muted); font-size: 0.8125rem; }
    .resting-orders { font-size: 0.78rem; overflow-x: auto; }
    .resting-orders table { width: 100%; border-collapse: collapse; }
    .resting-orders th, .resting-orders td { padding: 0.35rem 0.5rem; text-align: left; border-bottom: 1px solid #2a3544; font-variant-numeric: tabular-nums; }
    .resting-orders th { color: var(--muted); font-weight: 600; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.04em; }
    .resting-orders code { font-size: 0.72rem; }
    .chart-wrap { background: var(--card); border-radius: 8px; padding: 0.75rem 1rem; margin-bottom: 1.25rem; max-width: 100%; box-sizing: border-box; }
    .chart-wrap--chart-only { overflow: hidden; }
    .chart-wrap h2 { font-size: 0.9375rem; font-weight: 600; margin: 0 0 0.35rem; color: var(--muted); }
    .chart-panel { width: 100%; max-width: 100%; overflow: hidden; }
    .chart-scroll-outer {
      width: 100%; max-width: 100%; overflow-x: auto; overflow-y: hidden; border-radius: 6px;
      -webkit-overflow-scrolling: touch;
    }
    .chart-scroll-inner {
      position: relative; height: 220px; min-width: 100%;
    }
    .chart-hint { font-size: 0.75rem; color: var(--muted); margin: 0 0 0.5rem; line-height: 1.45; }
    .chart-hint button {
      font-size: 0.72rem; padding: 0.2rem 0.45rem; border-radius: 4px; border: 1px solid #2a3544;
      background: #1e2838; color: var(--text); cursor: pointer; font-weight: 600;
    }
    .chart-hint button:hover { background: #243044; }
    .chart-scroll-inner canvas { display: block; max-height: 220px; }
    .trade-feed { display: flex; flex-direction: column; gap: 0.65rem; max-width: 52rem; }
    .trade-card {
      border-radius: 10px; padding: 0.75rem 1rem; border: 1px solid #2a3544;
      background: linear-gradient(145deg, #1e2838 0%, #1a2332 100%);
      box-shadow: 0 2px 8px rgba(0,0,0,0.25);
    }
    .trade-card--positive {
      border-color: rgba(62,207,142,0.45);
      background: linear-gradient(145deg, rgba(62,207,142,0.14) 0%, #1a2332 55%);
    }
    .trade-card--negative {
      border-color: rgba(242,81,81,0.45);
      background: linear-gradient(145deg, rgba(242,81,81,0.12) 0%, #1a2332 55%);
    }
    .trade-card--neutral { border-color: #2e3d52; }
    .trade-card--buy { border-left: 3px solid #6eb5ff; }
    .trade-card--warn { border-left: 3px solid var(--warn); }
    .trade-card--err { border-left: 3px solid var(--err); }
    .trade-card__head { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem 0.75rem; margin-bottom: 0.4rem; }
    .trade-card__time { font-size: 0.75rem; color: var(--muted); font-variant-numeric: tabular-nums; }
    .trade-card__badge {
      font-family: ui-monospace, monospace; font-size: 0.7rem; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.04em;
      padding: 0.2rem 0.45rem; border-radius: 4px; background: #243044; color: #b8c5d6;
    }
    .trade-card__badge--cat {
      text-transform: none; letter-spacing: 0.02em; background: #2a2438; color: #d4c4f7;
      font-weight: 500; max-width: 12rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .trade-card__shares { font-size: 1rem; font-weight: 600; color: #e7ecf3; margin: 0 0 0.35rem; font-variant-numeric: tabular-nums; }
    .trade-card__market { font-size: 0.9rem; color: var(--text); line-height: 1.35; margin-bottom: 0.35rem; }
    .trade-card__meta { font-size: 0.8rem; color: #a8b4c5; display: flex; flex-wrap: wrap; gap: 0.35rem 1rem; }
    .trade-card__meta span { font-variant-numeric: tabular-nums; }
    .trade-card__pl {
      margin-top: 0.45rem; font-size: 0.95rem; font-weight: 600; font-variant-numeric: tabular-nums;
    }
    .trade-card__pl--pos { color: #5ee4a8; }
    .trade-card__pl--neg { color: #ff8a8a; }
    .trade-card__pl--zero { color: var(--muted); }
    .trade-card__detail { margin-top: 0.5rem; font-size: 0.7rem; color: var(--muted); max-height: 5rem; overflow: auto; }
    .trade-card__detail pre { margin: 0; white-space: pre-wrap; word-break: break-word; }
    @media (max-width: 900px) {
      body { flex-direction: column; }
      .sidebar { width: 100%; border-right: none; border-bottom: 1px solid #2a3544; max-height: 50vh; }
      .sidebar.collapsed { max-height: 0; margin-left: 0; width: 100%; padding: 0; border: none; }
    }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js"></script>
</head>
<body>
  <aside id="sidebar" class="sidebar">
    <div class="sidebar__head">
      <button type="button" id="sidebarToggle" title="Collapse sidebar">☰</button>
      <strong>Controller</strong>
    </div>
    <div class="sidebar__body">
      <div class="sell-loop-top">
        <h3>Auto-sell loop</h3>
        <button type="button" id="sellLoopToggle" class="sell-loop-pill" role="switch" aria-checked="false" aria-label="Toggle batch exit loop">
          <span class="sell-loop-pill__text">
            <span class="sell-loop-pill__title" id="sellLoopTitle">Off</span>
            <span class="sell-loop-pill__sub" id="sellLoopSub">Idle · localhost only</span>
          </span>
          <span class="sell-loop-pill__track" aria-hidden="true"><span class="sell-loop-pill__thumb"></span></span>
        </button>
      </div>
      <p class="sidebar__hint">Live controls apply to <strong>this</strong> process only (run with <code>--web</code>). Restart the bot to pick up <code>.env</code> edits.</p>
      <label>Buy size multiplier</label>
      <div class="seg" id="multSeg" role="group" aria-label="Order size multiplier">
        <button type="button" data-m="1">1×</button>
        <button type="button" data-m="2">2×</button>
        <button type="button" data-m="5">5×</button>
        <button type="button" data-m="10">10×</button>
      </div>
      <p class="sidebar__stat">Active: <strong id="multActive">1×</strong> · base contracts from strategy × multiplier (caps still apply).</p>
      <label>Settings snapshot</label>
      <pre id="settingsSnap" class="sidebar__pre">Loading…</pre>
      <div class="open-pos">
        <h3>Open positions</h3>
        <p class="sidebar__hint" style="margin:0 0 0.35rem;">Green/red = bid vs estimated entry. TP/stop from <code>TRADE_EXIT_*</code> (same idea as auto-sell; trailing/hold not shown). Buttons: localhost only.</p>
        <p id="posStatus"></p>
        <div id="openPositions">Loading…</div>
      </div>
    </div>
  </aside>
  <div class="main-wrap">
  <main class="main" style="max-width:100%;min-width:0;overflow-x:hidden;">
  <h1>Kalshi bot — trading monitor</h1>
  <p class="sub">YES positions as <strong>shares</strong> (Kalshi contracts) at an implied <strong>share price</strong> in ¢. Live feed of orders and blocks. Cash / positions / total use Kalshi&rsquo;s balance API (<strong>portfolio_value</strong> for mark-to-market positions — same notion as the app), not the sum of per-market <code>market_exposure</code> fields.</p>
  <div class="status" id="status">Loading…</div>
  <div class="status" id="wlBanner">W–L <strong id="wlStat">0–0</strong> <span class="muted" id="wlTies"></span> <span class="muted">(auto-sell / exit-scan; P/L from entry estimate when available; separate CLI posts to this page)</span></div>
  <div class="balance-banner" id="balanceBanner" style="display:none;">
    <span><strong>Cash</strong> <span id="balCash">—</span></span>
    <span title="portfolio_value from GET /portfolio/balance (MTM; matches Kalshi app)"><strong>Positions</strong> <span id="balExp">—</span></span>
    <span title="portfolio_value + cash (when both known)"><strong>Total</strong> <span id="balTotal">—</span></span>
    <span class="muted" id="balTs"></span>
  </div>
  <div class="chart-wrap chart-wrap--chart-only">
    <h2>Cash, positions value (MTM) &amp; total account (USD)</h2>
    <p class="chart-hint">Drag on the chart to pan along the time axis · mouse wheel (or trackpad pinch) to zoom · scroll horizontally when the series is long. <button type="button" id="chartResetZoom">Reset zoom</button></p>
    <div class="chart-panel">
      <div class="chart-scroll-outer" id="chartScrollOuter">
        <div class="chart-scroll-inner" id="chartScrollInner">
          <canvas id="seriesChart" width="800" height="220"></canvas>
        </div>
      </div>
    </div>
  </div>
  <div class="chart-wrap">
    <h2>Trades &amp; orders</h2>
    <p class="sub" style="margin:0 0 0.75rem;">Exit P/L uses green / red hues. Buys (dry-run / live submit) use a blue accent. Expand <strong>Raw JSON</strong> for the full payload. If trading runs in a <strong>different terminal</strong> than the dashboard, set <code>DASHBOARD_INGEST_TRADE_EVENTS=true</code> (default) so orders POST to this page.</p>
    <div class="trade-feed" id="tradeFeed"></div>
  </div>
  <div class="chart-wrap">
    <h2>Open orders</h2>
    <p class="sub" style="margin:0 0 0.5rem;">Resting limit orders from Kalshi (refreshed every second).</p>
    <div class="resting-orders" id="restingOrders">Loading…</div>
  </div>
  </main>
  </div>
  <script>
    let seriesChart = null;
    const POLL_MS = 1000;
    let lastPassUnix = 0;
    function dollarsFromCents(c) { return (Number(c) || 0) / 100; }
    function renderTradeCards(events) {
      const feed = document.getElementById('tradeFeed');
      if (!feed) return;
      feed.innerHTML = '';
      const tradeKinds = /^(dry_run|live_submit|live_ack|blocked|refused|auto_sell_profit_estimate)$/;
      for (const ev of events) {
        const k = ev.kind || '';
        if (!tradeKinds.test(k)) continue;
        const intent = ev.intent && typeof ev.intent === 'object' ? ev.intent : {};
        const ticker = ev.ticker || intent.ticker || '';
        const grossRaw = ev.estimated_gross_profit_cents;
        let tone = 'neutral';
        if (k === 'auto_sell_profit_estimate' && grossRaw != null && grossRaw !== '') {
          const g = Number(grossRaw);
          if (g > 0) tone = 'positive';
          else if (g < 0) tone = 'negative';
          else tone = 'neutral';
        }
        const card = document.createElement('article');
        card.className = 'trade-card trade-card--' + tone;
        if (k === 'dry_run' || k === 'live_submit') card.classList.add('trade-card--buy');
        if (k === 'blocked') card.classList.add('trade-card--warn');
        if (k === 'refused') card.classList.add('trade-card--err');

        const head = document.createElement('div');
        head.className = 'trade-card__head';
        const tm = document.createElement('span');
        tm.className = 'trade-card__time';
        tm.textContent = ev.ts_iso || '';
        const badge = document.createElement('span');
        badge.className = 'trade-card__badge';
        badge.textContent = (k || 'event').replace(/_/g, ' ');
        head.appendChild(tm);
        head.appendChild(badge);
        const catStr = (ev.market_category || '').trim();
        if (catStr) {
          const catBadge = document.createElement('span');
          catBadge.className = 'trade-card__badge trade-card__badge--cat';
          catBadge.title = 'Kalshi event category';
          catBadge.textContent = catStr;
          head.appendChild(catBadge);
        }
        card.appendChild(head);

        const market = document.createElement('div');
        market.className = 'trade-card__market';
        const title = (ev.market_title || '').trim();
        market.textContent = title || (ticker ? '— ' + ticker : '—');
        card.appendChild(market);

        const cntRaw = ev.order_contracts != null && ev.order_contracts !== '' ? ev.order_contracts
          : (ev.count != null && ev.count !== '' ? ev.count : (ev.shares != null && ev.shares !== '' ? ev.shares : intent.count));
        if (cntRaw != null && cntRaw !== '' && !isNaN(Number(cntRaw))) {
          const shr = document.createElement('div');
          shr.className = 'trade-card__shares';
          shr.textContent = Number(cntRaw) + ' shares';
          card.appendChild(shr);
        }

        const meta = document.createElement('div');
        meta.className = 'trade-card__meta';
        const bits = [];
        if (ticker) bits.push('Ticker ' + ticker);
        const yp = ev.yes_price_cents != null ? ev.yes_price_cents : intent.yes_price_cents;
        const lim = ev.limit_yes_price_cents;
        const price = lim != null ? lim : yp;
        if (price != null) bits.push('@ ' + price + '¢ YES');
        if (intent.side && intent.action) bits.push(intent.side + ' ' + intent.action);
        if (ev.entry_yes_cents != null) bits.push('entry ~' + ev.entry_yes_cents + '¢');
        if (ev.reason) bits.push(String(ev.reason));
        if (ev.env) bits.push('env ' + ev.env);
        if (ev.order_id) bits.push('order ' + ev.order_id);
        meta.textContent = bits.length ? bits.join(' · ') : '—';
        card.appendChild(meta);

        if (k === 'auto_sell_profit_estimate' && grossRaw != null && grossRaw !== '') {
          const g = Number(grossRaw);
          const pl = document.createElement('div');
          pl.className = 'trade-card__pl ' + (g > 0 ? 'trade-card__pl--pos' : g < 0 ? 'trade-card__pl--neg' : 'trade-card__pl--zero');
          const sign = g > 0 ? '+' : '';
          pl.textContent = 'Est. gross P/L ' + sign + (g / 100).toFixed(2) + ' USD (before fees)';
          card.appendChild(pl);
        }

        const det = document.createElement('details');
        det.className = 'trade-card__detail';
        const summ = document.createElement('summary');
        summ.textContent = 'Raw JSON';
        const pre = document.createElement('pre');
        pre.textContent = JSON.stringify(ev, null, 2);
        det.appendChild(summ);
        det.appendChild(pre);
        card.appendChild(det);
        feed.appendChild(card);
      }
      if (feed.children.length === 0) {
        const empty = document.createElement('p');
        empty.className = 'sub';
        empty.style.margin = '0';
        empty.textContent = 'No trade events yet — dry-run, live orders, or auto-sell will appear here.';
        feed.appendChild(empty);
      }
    }
    function buildOrUpdateChart(points) {
      const labels = points.map(p => {
        const t = new Date((p.unix || 0) * 1000);
        return t.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      });
      const bal = points.map(p => dollarsFromCents(p.balance_cents));
      const exp = points.map(p => {
        const pv = p.positions_value_cents != null && p.positions_value_cents !== '' ? p.positions_value_cents : p.exposure_cents;
        return dollarsFromCents(pv);
      });
      const total = points.map(p => (p.total_account_cents != null ? dollarsFromCents(p.total_account_cents) : null));
      const ctx = document.getElementById('seriesChart');
      if (!seriesChart) {
        seriesChart = new Chart(ctx, {
          type: 'line',
          data: {
            labels,
            datasets: [
              { label: 'Cash', data: bal, borderColor: '#3ecf8e', backgroundColor: 'rgba(62,207,142,0.08)', tension: 0.2, fill: false, pointRadius: 0, spanGaps: false },
              { label: 'Positions value (MTM)', data: exp, borderColor: '#6eb5ff', backgroundColor: 'rgba(110,181,255,0.08)', tension: 0.2, fill: false, pointRadius: 0, spanGaps: false },
              { label: 'Total (cash + positions)', data: total, borderColor: '#f5a623', backgroundColor: 'rgba(245,166,35,0.08)', tension: 0.2, fill: false, pointRadius: 0, spanGaps: false }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: 'index', intersect: false },
            scales: {
              x: { ticks: { maxTicksLimit: 12, color: '#8b9aab' }, grid: { color: '#2a3544' } },
              y: { ticks: { color: '#8b9aab' }, grid: { color: '#2a3544' } }
            },
            plugins: {
              legend: { labels: { color: '#e7ecf3' } },
              zoom: {
                limits: {
                  x: { min: 'original', max: 'original' },
                  y: { min: 'original', max: 'original' },
                },
                pan: {
                  enabled: true,
                  mode: 'x',
                  modifierKey: null,
                },
                zoom: {
                  wheel: { enabled: true },
                  pinch: { enabled: true },
                  mode: 'x',
                },
              },
            },
          }
        });
      } else {
        seriesChart.data.labels = labels;
        seriesChart.data.datasets[0].data = bal;
        seriesChart.data.datasets[1].data = exp;
        seriesChart.data.datasets[2].data = total;
        seriesChart.update('none');
      }
      const scrollInner = document.getElementById('chartScrollInner');
      const scrollOuter = document.getElementById('chartScrollOuter');
      if (scrollInner && scrollOuter && points.length) {
        var pw = scrollOuter.clientWidth || 800;
        var minW = Math.max(pw, Math.min(12000, points.length * 10 + 100));
        scrollInner.style.minWidth = minW + 'px';
        if (seriesChart) seriesChart.resize();
      } else if (scrollInner) {
        scrollInner.style.minWidth = '';
        if (seriesChart) seriesChart.resize();
      }
    }
    async function poll() {
      try {
        const rs = await fetch('/api/stats', { cache: 'no-store' });
        const st = await rs.json();
        const w = Number(st.wins) || 0;
        const l = Number(st.losses) || 0;
        const t = Number(st.ties) || 0;
        const wlEl = document.getElementById('wlStat');
        const tiesEl = document.getElementById('wlTies');
        if (wlEl) wlEl.textContent = w + '–' + l;
        if (tiesEl) tiesEl.textContent = t > 0 ? '· BE ' + t : '';
      } catch (e) { /* ignore */ }
      try {
        const r = await fetch('/api/events', { cache: 'no-store' });
        const data = await r.json();
        document.getElementById('status').textContent = data.length + ' event(s) — last update ' + new Date().toISOString();
        renderTradeCards(data);
      } catch (e) {
        document.getElementById('status').textContent = 'Fetch error (events) — is the bot running?';
      }
      try {
        const sr = await fetch('/api/series', { cache: 'no-store' });
        const pts = await sr.json();
        if (pts.length > 0) {
          buildOrUpdateChart(pts);
          const last = pts[pts.length - 1];
          const cashEl = document.getElementById('balCash');
          const expEl = document.getElementById('balExp');
          const totalEl = document.getElementById('balTotal');
          const tsEl = document.getElementById('balTs');
          const banner = document.getElementById('balanceBanner');
          const pvRaw = last.positions_value_cents != null && last.positions_value_cents !== '' ? last.positions_value_cents : last.exposure_cents;
          const exp = dollarsFromCents(pvRaw);
          expEl.textContent = '$' + exp.toFixed(2);
          if (last.balance_known === false) {
            cashEl.textContent = 'n/a';
            if (totalEl) totalEl.textContent = 'n/a';
          } else {
            const cash = dollarsFromCents(last.balance_cents);
            cashEl.textContent = '$' + cash.toFixed(2);
            if (totalEl) {
              if (last.total_account_cents != null) {
                totalEl.textContent = '$' + dollarsFromCents(last.total_account_cents).toFixed(2);
              } else {
                totalEl.textContent = 'n/a';
              }
            }
          }
          if (last.ts_iso) tsEl.textContent = 'as of ' + last.ts_iso;
          else tsEl.textContent = '';
          banner.style.display = 'flex';
        }
      } catch (e) {
        const st = document.getElementById('status');
        if (!st.textContent.includes('Fetch error')) {
          st.textContent = 'Series fetch error — chart may be stale';
        }
      }
      try {
        const pr = await fetch('/api/pass_summary', { cache: 'no-store' });
        const ps = await pr.json();
        const u = Number(ps.updated_unix) || 0;
        if (u && u !== lastPassUnix) {
          lastPassUnix = u;
          const sr2 = await fetch('/api/series', { cache: 'no-store' });
          const pts2 = await sr2.json();
          if (pts2.length > 0) {
            buildOrUpdateChart(pts2);
            const last = pts2[pts2.length - 1];
            const cashEl = document.getElementById('balCash');
            const expEl = document.getElementById('balExp');
            const totalEl = document.getElementById('balTotal');
            const tsEl = document.getElementById('balTs');
            const banner = document.getElementById('balanceBanner');
            const pvRaw = last.positions_value_cents != null && last.positions_value_cents !== '' ? last.positions_value_cents : last.exposure_cents;
            const exp = dollarsFromCents(pvRaw);
            expEl.textContent = '$' + exp.toFixed(2);
            if (last.balance_known === false) {
              cashEl.textContent = 'n/a';
              if (totalEl) totalEl.textContent = 'n/a';
            } else {
              const cash = dollarsFromCents(last.balance_cents);
              cashEl.textContent = '$' + cash.toFixed(2);
              if (totalEl) {
                if (last.total_account_cents != null) {
                  totalEl.textContent = '$' + dollarsFromCents(last.total_account_cents).toFixed(2);
                } else {
                  totalEl.textContent = 'n/a';
                }
              }
            }
            if (last.ts_iso) tsEl.textContent = 'as of ' + last.ts_iso;
            else tsEl.textContent = '';
            banner.style.display = 'flex';
          }
        }
      } catch (e) { /* ignore */ }
      refreshPositions();
      refreshRestingOrders();
      refreshSellLoopStatus();
    }
    async function refreshSellLoopStatus() {
      try {
        const r = await fetch('/api/sell_loop/status', { cache: 'no-store' });
        const d = await r.json();
        const running = !!d.running;
        const btn = document.getElementById('sellLoopToggle');
        const titleEl = document.getElementById('sellLoopTitle');
        const subEl = document.getElementById('sellLoopSub');
        if (btn) {
          btn.classList.toggle('is-on', running);
          btn.setAttribute('aria-checked', running ? 'true' : 'false');
        }
        if (titleEl) titleEl.textContent = running ? 'On' : 'Off';
        if (subEl) subEl.textContent = running ? 'Running — batch exit scan' : 'Idle · localhost only';
      } catch (e) { /* ignore */ }
    }
    function renderRestingOrders(data) {
      const wrap = document.getElementById('restingOrders');
      if (!wrap) return;
      wrap.innerHTML = '';
      if (data && data.error) {
        const p = document.createElement('p');
        p.className = 'sub';
        p.style.margin = '0';
        p.textContent = String(data.error);
        wrap.appendChild(p);
        return;
      }
      const rows = (data && data.orders) ? data.orders : [];
      if (!rows.length) {
        const p = document.createElement('p');
        p.className = 'sub';
        p.style.margin = '0';
        p.textContent = 'No resting orders.';
        wrap.appendChild(p);
        return;
      }
      const tbl = document.createElement('table');
      const thead = document.createElement('thead');
      const hr = document.createElement('tr');
      ['Ticker', 'Side', 'Action', 'Remain', 'Limit ¢', 'Order id'].forEach(function(h) {
        const th = document.createElement('th');
        th.textContent = h;
        hr.appendChild(th);
      });
      thead.appendChild(hr);
      tbl.appendChild(thead);
      const tb = document.createElement('tbody');
      for (const o of rows) {
        const tr = document.createElement('tr');
        const td1 = document.createElement('td');
        td1.innerHTML = '<code>' + String(o.ticker || '') + '</code>';
        const td2 = document.createElement('td');
        td2.textContent = String(o.side || '');
        const td3 = document.createElement('td');
        td3.textContent = String(o.action || '');
        const td4 = document.createElement('td');
        const rem = o.remaining_count != null ? o.remaining_count : o.count;
        td4.textContent = rem != null ? String(rem) : '—';
        const td5 = document.createElement('td');
        td5.textContent = o.yes_price != null ? String(o.yes_price) : '—';
        const td6 = document.createElement('td');
        td6.style.fontSize = '0.7rem';
        td6.textContent = String(o.order_id || '').slice(0, 18) + (String(o.order_id || '').length > 18 ? '…' : '');
        tr.appendChild(td1);
        tr.appendChild(td2);
        tr.appendChild(td3);
        tr.appendChild(td4);
        tr.appendChild(td5);
        tr.appendChild(td6);
        tb.appendChild(tr);
      }
      tbl.appendChild(tb);
      wrap.appendChild(tbl);
    }
    async function refreshRestingOrders() {
      try {
        const r = await fetch('/api/resting_orders', { cache: 'no-store' });
        const data = await r.json();
        renderRestingOrders(data);
      } catch (e) {
        renderRestingOrders({ orders: [], error: 'Could not load resting orders' });
      }
    }
    document.getElementById('sidebarToggle').addEventListener('click', function() {
      document.getElementById('sidebar').classList.toggle('collapsed');
    });
    function renderOpenPositions(data) {
      const wrap = document.getElementById('openPositions');
      const st = document.getElementById('posStatus');
      if (!wrap) return;
      wrap.innerHTML = '';
      if (data && data.error) {
        if (st) st.textContent = String(data.error);
      } else if (st) { st.textContent = ''; }
      const pos = (data && data.positions) ? data.positions : [];
      const ddGlob = data && data.double_down_enabled;
      const mult = Math.max(1, Number(data && data.order_size_multiplier) || 1);
      if (!pos.length) {
        const p = document.createElement('p');
        p.className = 'sidebar__hint';
        p.style.margin = '0';
        p.textContent = (data && data.error) ? '' : 'No long YES positions.';
        wrap.appendChild(p);
        return;
      }
      for (const row of pos) {
        const card = document.createElement('div');
        card.className = 'mini-pos';
        const sign = row.pnl_sign || 'unknown';
        if (sign === 'positive') card.classList.add('mini-pos--pos');
        else if (sign === 'negative') card.classList.add('mini-pos--neg');
        else if (sign === 'neutral') card.classList.add('mini-pos--neutral');
        const title = document.createElement('div');
        title.className = 'mini-pos__title';
        title.textContent = (row.market_title || '').trim() || row.ticker || '—';
        const shEl = document.createElement('div');
        shEl.className = 'mini-pos__shares';
        const shRaw = row.shares != null && row.shares !== '' ? Number(row.shares) : NaN;
        const shn = !isNaN(shRaw) ? Math.round(shRaw) : null;
        shEl.textContent = shn != null ? (shn + ' share' + (shn === 1 ? '' : 's')) : 'Shares —';
        const tk = document.createElement('div');
        tk.className = 'mini-pos__ticker';
        tk.textContent = row.ticker || '';
        const r1 = document.createElement('div');
        r1.className = 'mini-pos__row';
        const e = row.entry_yes_cents != null ? row.entry_yes_cents + '¢' : '—';
        const bid = row.best_yes_bid_cents != null ? row.best_yes_bid_cents + '¢' : '—';
        const ask = row.lift_yes_ask_cents != null ? row.lift_yes_ask_cents + '¢' : '—';
        const sp1 = document.createElement('span');
        sp1.textContent = 'Entry ~' + e;
        const sp2 = document.createElement('span');
        sp2.textContent = 'Bid ' + bid + ' · Ask ' + ask;
        r1.appendChild(sp1);
        r1.appendChild(sp2);
        const ud = row.unrealized_delta_cents;
        const exitEl = document.createElement('div');
        exitEl.className = 'mini-pos__exit';
        const levels = row.take_profit_levels_cents;
        const bidN = row.best_yes_bid_cents != null ? Number(row.best_yes_bid_cents) : null;
        const nextTp = row.take_profit_next_bid_cents != null && row.take_profit_next_bid_cents !== '' ? Number(row.take_profit_next_bid_cents) : null;
        const gapTp = row.cents_to_take_profit != null && row.cents_to_take_profit !== '' ? Number(row.cents_to_take_profit) : null;
        let tpLine = '';
        if (levels && levels.length) {
          const maxLv = Math.max.apply(null, levels);
          if (nextTp != null && !isNaN(nextTp)) {
            tpLine = 'Next TP: bid ≥ ' + nextTp + '¢ · ' + (gapTp != null && !isNaN(gapTp) ? gapTp + '¢ to go' : '');
          } else if (bidN != null && bidN >= maxLv) {
            tpLine = 'TP: bid at/above max rule (' + maxLv + '¢)';
          } else {
            tpLine = 'TP levels (¢): ' + levels.join(', ');
          }
        } else {
          tpLine = 'TP: add TRADE_EXIT rules (min profit / mult / implied %)';
        }
        const tpSpan = document.createElement('div');
        const tpLbl = document.createElement('span');
        tpLbl.className = 'tp';
        tpLbl.textContent = 'Take-profit · ';
        tpSpan.appendChild(tpLbl);
        tpSpan.appendChild(document.createTextNode(tpLine));
        exitEl.appendChild(tpSpan);
        const stopBid = row.stop_loss_bid_cents;
        const slSt = row.stop_loss_status || 'off';
        if (slSt === 'active' && stopBid != null && stopBid !== '') {
          const buf = row.cents_buffer_to_stop;
          const slDiv = document.createElement('div');
          const slLbl = document.createElement('span');
          slLbl.className = 'sl';
          slLbl.textContent = 'Stop-loss · ';
          let slRest = '';
          if (buf != null && buf !== '' && Number(buf) < 0) {
            slRest = 'bid ≤ ' + stopBid + '¢ triggers — bid at/below floor';
          } else if (buf != null && buf !== '') {
            slRest = 'fires if bid ≤ ' + stopBid + '¢ · buffer +' + buf + '¢';
          } else {
            slRest = 'fires if bid ≤ ' + stopBid + '¢';
          }
          slDiv.appendChild(slLbl);
          slDiv.appendChild(document.createTextNode(slRest));
          exitEl.appendChild(slDiv);
        } else {
          const slDiv = document.createElement('div');
          const slLbl = document.createElement('span');
          slLbl.className = 'sl';
          slLbl.textContent = 'Stop-loss · ';
          const msg = slSt === 'skipped_suspect'
            ? 'not shown for suspect portfolio entry (≈1¢/99¢)'
            : (slSt === 'off' ? 'disabled (TRADE_EXIT_STOP_LOSS_ENABLED=false)'
              : (slSt === 'no_entry' ? 'set entry (manual or portfolio) for floor' : 'n/a'));
          slDiv.appendChild(slLbl);
          slDiv.appendChild(document.createTextNode(msg));
          exitEl.appendChild(slDiv);
        }
        const actions = document.createElement('div');
        actions.className = 'mini-pos__actions';
        const bSell = document.createElement('button');
        bSell.type = 'button';
        bSell.className = 'sell';
        bSell.textContent = 'Sell';
        bSell.addEventListener('click', function() { positionAction(row.ticker, 'sell'); });
        const bDd = document.createElement('button');
        bDd.type = 'button';
        bDd.className = 'dd';
        bDd.textContent = 'Double down';
        const room = Number(row.double_down_room) || 0;
        const ddOk = ddGlob && room >= mult;
        bDd.disabled = !ddOk;
        bDd.title = !ddGlob ? 'TRADE_DOUBLE_DOWN_ENABLED=false' : (room < mult ? 'Need headroom ≥ buy multiplier (room ' + room + ' < ' + mult + '×)' : '');
        bDd.addEventListener('click', function() { positionAction(row.ticker, 'double_down'); });
        actions.appendChild(bSell);
        actions.appendChild(bDd);
        card.appendChild(title);
        card.appendChild(shEl);
        card.appendChild(tk);
        card.appendChild(r1);
        if (row.entry_yes_cents != null && row.best_yes_bid_cents != null && ud != null && ud !== '') {
          const pl = document.createElement('div');
          pl.className = 'mini-pos__pl ' + (ud > 0 ? 'mini-pos__pl--pos' : ud < 0 ? 'mini-pos__pl--neg' : 'mini-pos__pl--zero');
          const sgn = ud > 0 ? '+' : '';
          pl.textContent = 'Mark vs entry: ' + sgn + ud + '¢ (bid − entry)';
          card.appendChild(pl);
        }
        card.appendChild(exitEl);
        card.appendChild(actions);
        wrap.appendChild(card);
      }
    }
    async function refreshPositions() {
      try {
        const r = await fetch('/api/positions', { cache: 'no-store' });
        const data = await r.json();
        renderOpenPositions(data);
      } catch (e) {
        renderOpenPositions({ positions: [], error: 'Could not load positions (is the bot running?)' });
      }
    }
    async function positionAction(ticker, action) {
      const st = document.getElementById('posStatus');
      if (st) st.textContent = 'Submitting…';
      try {
        const r = await fetch('/api/positions/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ticker: ticker, action: action }),
        });
        const j = await r.json().catch(function() { return {}; });
        if (!r.ok || !j.ok) {
          if (st) st.textContent = (j && j.error) ? String(j.error) : ('HTTP ' + r.status);
          return;
        }
        if (st) st.textContent = j.detail ? String(j.detail) : 'OK';
        await refreshPositions();
      } catch (e) {
        if (st) st.textContent = 'Network error';
      }
    }
    async function refreshControl() {
      try {
        const r = await fetch('/api/control', { cache: 'no-store' });
        const d = await r.json();
        const m = Number(d.order_size_multiplier) || 1;
        document.getElementById('multActive').textContent = m + '×';
        document.querySelectorAll('#multSeg button').forEach(function(btn) {
          btn.classList.toggle('active', Number(btn.getAttribute('data-m')) === m);
        });
        const snap = d.settings_snapshot || {};
        document.getElementById('settingsSnap').textContent = JSON.stringify(snap, null, 2);
      } catch (e) {
        document.getElementById('settingsSnap').textContent = '(could not load /api/control)';
      }
    }
    document.querySelectorAll('#multSeg button').forEach(function(btn) {
      btn.addEventListener('click', async function() {
        const m = Number(btn.getAttribute('data-m')) || 1;
        try {
          const r = await fetch('/api/control', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order_size_multiplier: m }),
          });
          if (r.ok) await refreshControl();
        } catch (e) { /* ignore */ }
      });
    });
    var chartResetBtn = document.getElementById('chartResetZoom');
    if (chartResetBtn) {
      chartResetBtn.addEventListener('click', function() {
        if (seriesChart && typeof seriesChart.resetZoom === 'function') seriesChart.resetZoom();
      });
    }
    var sellLoopToggle = document.getElementById('sellLoopToggle');
    if (sellLoopToggle) {
      sellLoopToggle.addEventListener('click', async function() {
        const on = sellLoopToggle.getAttribute('aria-checked') === 'true';
        try {
          await fetch(on ? '/api/sell_loop/stop' : '/api/sell_loop/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
          });
        } catch (e) { /* ignore */ }
        refreshSellLoopStatus();
      });
    }
    poll();
    refreshControl();
    refreshPositions();
    refreshRestingOrders();
    refreshSellLoopStatus();
    setInterval(poll, POLL_MS);
    setInterval(refreshControl, 8000);
  </script>
</body>
</html>"""


def _create_app() -> Any:
    from flask import Flask, Response, jsonify, request

    from kalshi_bot.log_insights import aggregate_structured_log_tail

    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        return Response(_HTML, mimetype="text/html")

    @app.post("/api/ingest_auto_sell")
    def ingest_auto_sell() -> Any:
        """Accept auto-sell outcome from another process (e.g. ``kalshi-bot auto-sell``); localhost only."""
        addr = request.environ.get("REMOTE_ADDR", "")
        if addr not in ("127.0.0.1", "::1"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        data = request.get_json(force=True, silent=True) or {}
        gross = data.get("gross_profit_cents")
        if gross is not None:
            try:
                gross = int(gross)
            except (TypeError, ValueError):
                gross = None
        reason = str(data.get("exit_reason") or "")
        payload = data.get("event_payload")
        if not isinstance(payload, dict):
            payload = {}
        _apply_auto_sell_outcome_and_event(
            gross_profit_cents=gross,
            exit_reason=reason,
            event_payload=payload,
        )
        return jsonify({"ok": True})

    @app.post("/api/ingest_event")
    def ingest_event() -> Any:
        """Append one trade/order row from another process (e.g. bot without ``--web``); localhost only."""
        addr = request.environ.get("REMOTE_ADDR", "")
        if addr not in ("127.0.0.1", "::1"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        data = request.get_json(force=True, silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "bad_json"}), 400
        kind = str(data.get("kind") or "")
        if kind not in _INGESTABLE_EVENT_KINDS:
            return jsonify({"ok": False, "error": "bad_kind"}), 400
        row = dict(data)
        row["kind"] = kind
        _append_event_row(row)
        return jsonify({"ok": True})

    @app.post("/api/ingest_portfolio_series")
    def ingest_portfolio_series() -> Any:
        """Append one chart point by fetching portfolio from Kalshi (localhost only; dashboard needs API keys)."""
        addr = request.environ.get("REMOTE_ADDR", "")
        if addr not in ("127.0.0.1", "::1"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        from kalshi_bot.config import get_settings as _gs

        s = _gs()
        if not getattr(s, "dashboard_ingest_portfolio_series", True):
            return jsonify({"ok": False, "error": "disabled"}), 400
        try:
            from kalshi_bot.trading import build_sdk_client as _bsc

            client = _bsc(s)
            snap = fetch_portfolio_snapshot(client, ticker=None)
            record_portfolio_series_point(
                snap.balance_cents,
                snap.portfolio_value_cents,
                exposure_sum_cents=float(snap.total_exposure_cents),
            )
            return jsonify({"ok": True})
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("ingest_portfolio_series_failed", error=str(exc))
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.get("/api/events")
    def api_events() -> Any:
        with _LOCK:
            return jsonify(list(_EVENTS))

    @app.get("/api/series")
    def api_series() -> Any:
        with _LOCK:
            return jsonify(list(_SERIES))

    @app.get("/api/resting_orders")
    def api_resting_orders() -> Any:
        """Resting limit orders from Kalshi (same account as dashboard API keys)."""
        from kalshi_bot.config import get_settings as _gs

        try:
            from kalshi_bot.trading import build_sdk_client as _bsc

            s = _gs()
            client = _bsc(s)
            orders = list_resting_orders_detail(client)
            return jsonify({"orders": orders})
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("api_resting_orders_failed", error=str(exc))
            return jsonify({"orders": [], "error": str(exc)})

    @app.get("/api/stats")
    def api_stats() -> Any:
        return jsonify(win_loss_snapshot())

    @app.get("/api/log_summary")
    def api_log_summary() -> Any:
        """Structured JSONL tail stats for scripts / old clients — not rendered on the dashboard page."""
        path = structured_log_path_for_dashboard()
        payload = aggregate_structured_log_tail(path)
        _LOG.debug("log_summary_served", extra={"log_path": str(path)})
        return jsonify(payload)

    @app.get("/api/pass_summary")
    def api_pass_summary() -> Any:
        """Last llm/discover/tape/bitcoin pass counters — updated by CLI; not rendered on the dashboard page."""
        with _LOCK:
            body = dict(_LAST_PASS_SUMMARY) if _LAST_PASS_SUMMARY else {}
        _LOG.debug("pass_summary_served", extra={"has_data": bool(body)})
        return jsonify(body)

    def _request_localhost() -> bool:
        return request.environ.get("REMOTE_ADDR", "") in ("127.0.0.1", "::1")

    @app.post("/api/sell_loop/start")
    def sell_loop_start() -> Any:
        """Start batch exit loop in this process (localhost only)."""
        if not _request_localhost():
            return jsonify({"ok": False, "error": "forbidden"}), 403
        global _SELL_LOOP_THREAD
        with _SELL_LOOP_LOCK:
            if _SELL_LOOP_THREAD is not None and _SELL_LOOP_THREAD.is_alive():
                return jsonify({"ok": True, "running": True, "already": True})
            _SELL_LOOP_STOP.clear()
            t = threading.Thread(target=_dashboard_sell_loop_worker, name="dashboard-sell-loop", daemon=True)
            t.start()
            _SELL_LOOP_THREAD = t
        return jsonify({"ok": True, "running": True})

    @app.post("/api/sell_loop/stop")
    def sell_loop_stop() -> Any:
        if not _request_localhost():
            return jsonify({"ok": False, "error": "forbidden"}), 403
        global _SELL_LOOP_THREAD
        _SELL_LOOP_STOP.set()
        th = _SELL_LOOP_THREAD
        if th is not None and th.is_alive():
            th.join(timeout=10.0)
        return jsonify({"ok": True, "running": False})

    @app.get("/api/sell_loop/status")
    def sell_loop_status() -> Any:
        alive = _SELL_LOOP_THREAD is not None and _SELL_LOOP_THREAD.is_alive()
        return jsonify({"running": bool(alive)})

    @app.get("/api/control")
    def api_control_get() -> Any:
        """Live session controls + read-only settings snapshot (same process as ``--web``)."""
        from kalshi_bot.config import get_settings
        from kalshi_bot.runtime_controls import get_order_size_multiplier

        s = get_settings()
        odm = max(1, int(get_order_size_multiplier()))
        snap = {
            "trade_min_net_edge_after_fees": s.trade_min_net_edge_after_fees,
            "trade_double_down_enabled": s.trade_double_down_enabled,
            "trade_spike_fade_enabled": s.trade_spike_fade_enabled,
            "trade_llm_auto_execute": s.trade_llm_auto_execute,
            "dry_run": s.dry_run,
            "live_trading": s.live_trading,
            "max_contracts_per_market": s.max_contracts_per_market,
            "effective_max_contracts_per_market_cap": int(s.max_contracts_per_market) * odm,
            "strategy_order_count": s.strategy_order_count,
            "kalshi_env": s.kalshi_env,
            "trade_entry_hard_max_yes_ask_cents": s.trade_entry_hard_max_yes_ask_cents,
            "trade_entry_effective_max_yes_ask_dollars": s.trade_entry_effective_max_yes_ask_dollars,
        }
        return jsonify(
            {
                "order_size_multiplier": get_order_size_multiplier(),
                "settings_snapshot": snap,
                "note": "Multiplier scales base order size and per-market contract cap (see effective_max_contracts_per_market_cap). Edit .env and restart for other changes.",
            }
        )

    @app.post("/api/control")
    def api_control_post() -> Any:
        """Set ``order_size_multiplier`` (1, 2, 5, or 10). Localhost only."""
        if not _request_localhost():
            return jsonify({"ok": False, "error": "forbidden"}), 403
        from kalshi_bot.runtime_controls import set_order_size_multiplier

        data = request.get_json(force=True, silent=True) or {}
        raw = data.get("order_size_multiplier", 1)
        try:
            m = int(raw)
        except (TypeError, ValueError):
            m = 1
        v = set_order_size_multiplier(m)
        _LOG.info("dashboard_control_multiplier", order_size_multiplier=v)
        return jsonify({"ok": True, "order_size_multiplier": v})

    @app.get("/api/positions")
    def api_positions() -> Any:
        """Long YES positions with entry estimate and best bid (read-only). Uses same entry logic as auto-sell."""
        from kalshi_bot.auto_sell import _resolve_entry_reference
        from kalshi_bot.config import get_settings
        from kalshi_bot.logger import get_logger
        from kalshi_bot.market_data import (
            best_yes_bid_cents,
            get_orderbook,
            lift_yes_ask_cents_from_orderbook,
            market_title_for_ticker,
        )
        from kalshi_bot.runtime_controls import get_order_size_multiplier
        from kalshi_bot.trading import build_sdk_client

        try:
            settings = get_settings()
            client = build_sdk_client(settings)
            log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
            odm = max(1, int(get_order_size_multiplier()))
            snap = fetch_portfolio_snapshot(client, ticker=None)
            positions: list[dict[str, Any]] = []
            for ticker in sorted(t for t, s in snap.positions_by_ticker.items() if float(s) > 0):
                signed = float(snap.positions_by_ticker.get(ticker, 0.0))
                entry_ref = _resolve_entry_reference(settings, client, ticker, log)
                ob = get_orderbook(client, ticker)
                bid = best_yes_bid_cents(ob)
                lift = lift_yes_ask_cents_from_orderbook(ob)
                held = int(round(signed))
                max_pos = int(settings.trade_double_down_max_position_contracts)
                room = max(0, max_pos - held)
                hints = dashboard_position_exit_hints(
                    settings,
                    entry_cents=entry_ref.cents,
                    entry_source=entry_ref.source,
                    best_bid_cents=bid,
                )
                row_d: dict[str, Any] = {
                    "ticker": ticker,
                    "shares": signed,
                    "entry_yes_cents": entry_ref.cents,
                    "entry_source": entry_ref.source,
                    "best_yes_bid_cents": bid,
                    "lift_yes_ask_cents": lift,
                    "market_title": market_title_for_ticker(client, ticker) or "",
                    "double_down_room": room,
                }
                row_d.update(hints)
                positions.append(row_d)
            return jsonify(
                {
                    "positions": positions,
                    "double_down_enabled": bool(settings.trade_double_down_enabled),
                    "order_size_multiplier": odm,
                }
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("api_positions_failed", error=str(exc))
            return jsonify(
                {
                    "positions": [],
                    "double_down_enabled": False,
                    "order_size_multiplier": 1,
                    "error": str(exc),
                }
            )

    @app.post("/api/positions/action")
    def api_positions_action() -> Any:
        """Sell all long YES at best bid (minus aggression) or double-down buy at lift YES ask. Localhost only."""
        if not _request_localhost():
            return jsonify({"ok": False, "error": "forbidden"}), 403
        from kalshi_bot.config import get_settings
        from kalshi_bot.execution import DryRunLedger
        from kalshi_bot.logger import get_logger
        from kalshi_bot.market_data import best_yes_bid_cents, get_orderbook, lift_yes_ask_cents_from_orderbook
        from kalshi_bot.risk import RiskManager
        from kalshi_bot.runtime_controls import get_order_size_multiplier
        from kalshi_bot.trading import build_sdk_client, make_limit_intent, trade_execute

        data = request.get_json(force=True, silent=True) or {}
        ticker = str(data.get("ticker") or "").strip()
        action = str(data.get("action") or "").strip().lower()
        if not ticker or action not in ("sell", "double_down"):
            return jsonify({"ok": False, "error": "bad_request"}), 400

        settings = get_settings()
        log = get_logger("kalshi_bot", log_path=settings.structured_log_path, level=settings.log_level)
        client = build_sdk_client(settings)
        risk = RiskManager(settings)
        ledger = DryRunLedger()

        snap = fetch_portfolio_snapshot(client, ticker=ticker)
        signed = float(snap.positions_by_ticker.get(ticker, 0.0))
        if signed <= 0:
            return jsonify({"ok": False, "error": "no_long_yes"}), 400

        try:
            if action == "sell":
                cnt = int(round(signed))
                if cnt < 1:
                    return jsonify({"ok": False, "error": "zero_contracts"}), 400
                ob = get_orderbook(client, ticker)
                best = best_yes_bid_cents(ob)
                if best is None:
                    return jsonify({"ok": False, "error": "no_yes_bids"}), 400
                limit_cents = max(1, int(best) - int(settings.trade_exit_sell_aggression_cents))
                tif = settings.trade_exit_sell_time_in_force
                intent = make_limit_intent(
                    ticker=ticker,
                    side="yes",
                    action="sell",
                    count=cnt,
                    yes_price_cents=limit_cents,
                    time_in_force=tif,
                )
                trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
                detail = f"sell {cnt} YES @ {limit_cents}¢ ({'dry-run' if settings.dry_run else 'live'})"
                _LOG.info("dashboard_position_sell", ticker=ticker, count=cnt, limit_yes_price_cents=limit_cents)
                return jsonify({"ok": True, "action": "sell", "detail": detail})

            if not settings.trade_double_down_enabled:
                return jsonify({"ok": False, "error": "double_down_disabled"}), 400
            import math

            from kalshi_bot.sizing import next_buy_yes_notional_min_max

            ob = get_orderbook(client, ticker)
            lift = lift_yes_ask_cents_from_orderbook(ob)
            if lift is None:
                return jsonify({"ok": False, "error": "no_lift_yes_ask"}), 400
            held = int(round(signed))
            mult = max(1, int(get_order_size_multiplier()))
            room = max(0, int(settings.trade_double_down_max_position_contracts) - held)
            # execute_intent multiplies buy count by mult afterward — cap pre-mult contracts so we stay within room.
            max_pre_mult = room // mult
            if max_pre_mult < 1:
                return jsonify({"ok": False, "error": "at_max_position"}), 400
            lift_price = int(lift)
            min_n, max_n = next_buy_yes_notional_min_max(settings, balance_cents=snap.balance_cents)
            p = max(0.01, min(0.99, lift_price / 100.0))
            min_final = 1
            if min_n is not None and float(min_n) > 0:
                min_final = max(1, int(math.ceil(float(min_n) / p)))
            max_final: int | None = None
            if max_n is not None and float(max_n) > 0:
                max_final = max(0, int(float(max_n) / p))
            if max_final is not None and min_final > max_final:
                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"min_order_notional: need at least {min_final} contracts at {lift_price}¢ for "
                            f"${float(min_n or 0):.2f} min notional, but per-order cap ${float(max_n):.2f} allows at most "
                            f"{max_final}. Raise TRADE_MAX_ORDER_NOTIONAL_USD or lower TRADE_MIN_ORDER_NOTIONAL_USD."
                        ),
                    }
                ), 400
            min_base = (min_final + mult - 1) // mult
            count = min(max_pre_mult, max(int(settings.strategy_order_count), min_base))
            if count < min_base:
                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"min_order_notional: need at least {min_base} base contract(s) before {mult}× at {lift_price}¢ "
                            f"to meet ${float(min_n or 0):.2f} min (room {max_pre_mult} base). "
                            "Increase STRATEGY_ORDER_COUNT, reduce dashboard multiplier, or increase "
                            "TRADE_DOUBLE_DOWN_MAX_POSITION_CONTRACTS."
                        ),
                    }
                ), 400
            if count < 1:
                return jsonify({"ok": False, "error": "at_max_position"}), 400
            intent = make_limit_intent(
                ticker=ticker,
                side="yes",
                action="buy",
                count=count,
                yes_price_cents=int(lift),
                time_in_force="good_till_canceled",
                double_down=True,
            )
            trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
            detail = f"buy {count} YES @ {lift}¢ double-down ({'dry-run' if settings.dry_run else 'live'})"
            _LOG.info(
                "dashboard_position_double_down",
                ticker=ticker,
                count=count,
                yes_price_cents=lift,
            )
            return jsonify({"ok": True, "action": "double_down", "detail": detail})
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("api_positions_action_failed", ticker=ticker, action=action)
            return jsonify({"ok": False, "error": str(exc)}), 500

    return app


def start_dashboard(settings: Settings) -> threading.Thread | None:
    """Start Flask in a daemon thread; optionally open the default browser."""
    if not settings.dashboard_enabled:
        return None

    def _run() -> None:
        app.run(
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            threaded=True,
            use_reloader=False,
        )

    th = threading.Thread(target=_run, name="kalshi-dashboard", daemon=True)
    th.start()
    time.sleep(0.4)
    url = f"http://{settings.dashboard_host}:{settings.dashboard_port}/"
    if settings.dashboard_open_browser:
        webbrowser.open(url)
    print(f"Monitor dashboard: {url}", flush=True)
    return th


def start_portfolio_series_poller(settings: Settings, client: KalshiSdkClient) -> threading.Thread | None:
    """Background balance/exposure snapshots so the chart updates during long LLM or tape passes (not only between iterations)."""
    interval = float(settings.dashboard_portfolio_poll_seconds)
    if interval <= 0 or not settings.dashboard_enabled:
        return None

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                snap = fetch_portfolio_snapshot(client, ticker=None)
                record_portfolio_series_point(
                    snap.balance_cents,
                    snap.portfolio_value_cents,
                    exposure_sum_cents=float(snap.total_exposure_cents),
                )
            except Exception:
                pass

    poller = threading.Thread(target=_loop, name="kalshi-series-poller", daemon=True)
    poller.start()
    return poller


def heartbeat(note: str = "") -> None:
    """Optional periodic ping so the page shows activity even without orders."""
    record_event("heartbeat", note=note or "running")


# WSGI entrypoint for gunicorn / uwsgi. Same Flask instance as ``start_dashboard`` (in-memory events).
app = _create_app()
