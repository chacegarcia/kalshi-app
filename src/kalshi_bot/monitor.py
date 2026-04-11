"""Local web dashboard to watch orders and risk events while the bot runs."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings
from kalshi_bot.portfolio import fetch_portfolio_snapshot

_LOCK = Lock()
_EVENTS: deque[dict[str, Any]] = deque(maxlen=500)
# Set when dashboard starts so /api/log_summary can locate JSONL without Settings in Flask context
_STRUCTURED_LOG_PATH_FOR_STATS: Path | None = None
# Time series for dashboard line chart (balance + exposure in cents)
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
    if exit_reason.startswith("stop_loss"):
        with _LOCK:
            _LOSSES += 1


def win_loss_snapshot() -> dict[str, int]:
    """Return current win/loss/tie counts (thread-safe copy)."""
    with _LOCK:
        return {"wins": _WINS, "losses": _LOSSES, "ties": _TIES}


def record_portfolio_series_point(balance_cents: int | float | None, exposure_cents: float) -> None:
    """Append one point for the live line chart (thread-safe)."""
    try:
        bal = int(balance_cents) if balance_cents is not None else 0
    except (TypeError, ValueError):
        bal = 0
    row = {
        "unix": time.time(),
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "balance_cents": bal,
        "balance_known": balance_cents is not None,
        "exposure_cents": float(exposure_cents),
    }
    with _LOCK:
        _SERIES.append(row)


def _json_safe(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    return str(obj)


def record_event(kind: str, **payload: Any) -> None:
    """Append one row for the dashboard (thread-safe)."""
    safe = {k: _json_safe(v) for k, v in payload.items()}
    row = {
        "kind": kind,
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "unix": time.time(),
        **safe,
    }
    with _LOCK:
        _EVENTS.appendleft(row)


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Kalshi bot monitor</title>
  <style>
    :root { --bg:#0f1419; --card:#1a2332; --text:#e7ecf3; --muted:#8b9aab; --ok:#3ecf8e; --warn:#f5a623; --err:#f25151; }
    * { box-sizing: border-box; }
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--text); margin:0; padding:1rem 1.25rem 2rem; }
    h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.5rem; }
    p.sub { color: var(--muted); font-size: 0.875rem; margin: 0 0 1rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
    th, td { text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid #2a3544; vertical-align: top; }
    th { color: var(--muted); font-weight: 500; position: sticky; top: 0; background: var(--bg); }
    tr:hover td { background: #141c26; }
    .kind { font-family: ui-monospace, monospace; font-size: 0.75rem; }
    .kind-dry_run { color: var(--ok); }
    .kind-live { color: #6eb5ff; }
    .kind-blocked { color: var(--warn); }
    .kind-refused { color: var(--err); }
    .kind-heartbeat { color: var(--muted); }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 0.75rem; color: #c5d0dc; }
    .status { display: inline-block; padding: 0.15rem 0.45rem; border-radius: 4px; background: var(--card); font-size: 0.75rem; color: var(--muted); margin-bottom: 0.75rem; }
    .balance-banner { display: flex; flex-wrap: wrap; gap: 0.5rem 1.25rem; align-items: baseline; background: var(--card); border-radius: 8px; padding: 0.65rem 1rem; margin-bottom: 0.75rem; font-size: 0.9375rem; }
    .balance-banner strong { color: var(--text); font-weight: 600; }
    .balance-banner span.muted { color: var(--muted); font-size: 0.8125rem; }
    .chart-wrap { background: var(--card); border-radius: 8px; padding: 0.75rem 1rem; margin-bottom: 1.25rem; max-width: 100%; }
    .chart-wrap h2 { font-size: 0.9375rem; font-weight: 600; margin: 0 0 0.5rem; color: var(--muted); }
    .chart-wrap canvas { max-height: 220px; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body>
  <h1>Kalshi bot — trading monitor</h1>
  <p class="sub">YES positions as <strong>shares</strong> (Kalshi contracts) at an implied <strong>share price</strong> in ¢. Live feed of orders and blocks; chart polls every 2s when the bot records balance/exposure snapshots.</p>
  <div class="status" id="status">Loading…</div>
  <div class="status" id="wlBanner">W–L <strong id="wlStat">0–0</strong> <span class="muted" id="wlTies"></span> <span class="muted">(auto-sell / exit-scan; P/L from entry estimate when available; separate CLI posts to this page)</span></div>
  <div class="balance-banner" id="balanceBanner" style="display:none;">
    <span><strong>Cash</strong> <span id="balCash">—</span></span>
    <span><strong>Exposure</strong> <span id="balExp">—</span></span>
    <span class="muted" id="balTs"></span>
  </div>
  <div class="chart-wrap">
    <h2>Balance &amp; exposure (USD)</h2>
    <canvas id="seriesChart" width="800" height="220"></canvas>
  </div>
  <div class="chart-wrap">
    <h2>Structured log tail (event counts)</h2>
    <p class="sub" style="margin:0 0 0.5rem;">Recent lines of <code>STRUCTURED_LOG_PATH</code> — which events fired most often.</p>
    <pre id="logSummaryPre" style="font-size:0.75rem;max-height:160px;overflow:auto;color:#c5d0dc;">—</pre>
  </div>
  <table>
    <thead><tr><th>Time (UTC)</th><th>Kind</th><th>Detail</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <script>
    let seriesChart = null;
    function dollarsFromCents(c) { return (Number(c) || 0) / 100; }
    function buildOrUpdateChart(points) {
      const labels = points.map(p => {
        const t = new Date((p.unix || 0) * 1000);
        return t.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      });
      const bal = points.map(p => dollarsFromCents(p.balance_cents));
      const exp = points.map(p => dollarsFromCents(p.exposure_cents));
      const ctx = document.getElementById('seriesChart');
      if (!seriesChart) {
        seriesChart = new Chart(ctx, {
          type: 'line',
          data: {
            labels,
            datasets: [
              { label: 'Balance', data: bal, borderColor: '#3ecf8e', backgroundColor: 'rgba(62,207,142,0.08)', tension: 0.2, fill: false, pointRadius: 0 },
              { label: 'Exposure', data: exp, borderColor: '#6eb5ff', backgroundColor: 'rgba(110,181,255,0.08)', tension: 0.2, fill: false, pointRadius: 0 }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: true,
            animation: false,
            interaction: { mode: 'index', intersect: false },
            scales: {
              x: { ticks: { maxTicksLimit: 8, color: '#8b9aab' }, grid: { color: '#2a3544' } },
              y: { ticks: { color: '#8b9aab' }, grid: { color: '#2a3544' } }
            },
            plugins: { legend: { labels: { color: '#e7ecf3' } } }
          }
        });
      } else {
        seriesChart.data.labels = labels;
        seriesChart.data.datasets[0].data = bal;
        seriesChart.data.datasets[1].data = exp;
        seriesChart.update('none');
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
        const tb = document.getElementById('rows');
        tb.innerHTML = '';
        for (const ev of data) {
          const tr = document.createElement('tr');
          const k = ev.kind || '';
          const cls = 'kind kind-' + k.replace(/[^a-z0-9_-]/gi, '_');
          const detail = document.createElement('td');
          const pre = document.createElement('pre');
          pre.textContent = JSON.stringify(ev, null, 2);
          tr.innerHTML = '<td>' + (ev.ts_iso || '') + '</td><td class="' + cls + '">' + k + '</td>';
          tr.appendChild(detail);
          detail.appendChild(pre);
          tb.appendChild(tr);
        }
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
          const tsEl = document.getElementById('balTs');
          const banner = document.getElementById('balanceBanner');
          const exp = dollarsFromCents(last.exposure_cents);
          expEl.textContent = '$' + exp.toFixed(2);
          if (last.balance_known === false) {
            cashEl.textContent = 'n/a';
          } else {
            const cash = dollarsFromCents(last.balance_cents);
            cashEl.textContent = '$' + cash.toFixed(2);
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
    }
    async function pollLogSummary() {
      try {
        const r = await fetch('/api/log_summary', { cache: 'no-store' });
        const j = await r.json();
        const el = document.getElementById('logSummaryPre');
        if (!el) return;
        const lines = j.top_events || [];
        el.textContent = lines.length ? lines.join('\\n') : (j.error || 'no lines parsed');
      } catch (e) { /* ignore */ }
    }
    poll();
    setInterval(poll, 2000);
    pollLogSummary();
    setInterval(pollLogSummary, 5000);
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

    @app.get("/api/events")
    def api_events() -> Any:
        with _LOCK:
            return jsonify(list(_EVENTS))

    @app.get("/api/series")
    def api_series() -> Any:
        with _LOCK:
            return jsonify(list(_SERIES))

    @app.get("/api/stats")
    def api_stats() -> Any:
        return jsonify(win_loss_snapshot())

    @app.get("/api/log_summary")
    def api_log_summary() -> Any:
        p = _STRUCTURED_LOG_PATH_FOR_STATS or Path("logs/kalshi_bot.jsonl")
        return jsonify(aggregate_structured_log_tail(p))

    return app


def start_dashboard(settings: Settings) -> threading.Thread | None:
    """Start Flask in a daemon thread; optionally open the default browser."""
    global _STRUCTURED_LOG_PATH_FOR_STATS
    if not settings.dashboard_enabled:
        return None
    _STRUCTURED_LOG_PATH_FOR_STATS = settings.structured_log_path

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
                record_portfolio_series_point(snap.balance_cents, float(snap.total_exposure_cents))
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
