"""Local web dashboard to watch orders and risk events while the bot runs."""

from __future__ import annotations

import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from kalshi_bot.config import Settings

_LOCK = Lock()
_EVENTS: deque[dict[str, Any]] = deque(maxlen=500)


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
  </style>
</head>
<body>
  <h1>Kalshi bot — order monitor</h1>
  <p class="sub">Live feed of intents, simulated orders, blocks, and live submits. Refreshes every 2s.</p>
  <div class="status" id="status">Loading…</div>
  <table>
    <thead><tr><th>Time (UTC)</th><th>Kind</th><th>Detail</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <script>
    async function poll() {
      try {
        const r = await fetch('/api/events');
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
        document.getElementById('status').textContent = 'Fetch error (is the bot running?)';
      }
    }
    poll();
    setInterval(poll, 2000);
  </script>
</body>
</html>"""


def _create_app() -> Any:
    from flask import Flask, Response, jsonify

    app = Flask(__name__)

    @app.get("/")
    def index() -> Response:
        return Response(_HTML, mimetype="text/html")

    @app.get("/api/events")
    def api_events() -> Any:
        with _LOCK:
            return jsonify(list(_EVENTS))

    return app


def start_dashboard(settings: Settings) -> threading.Thread | None:
    """Start Flask in a daemon thread; optionally open the default browser."""
    if not settings.dashboard_enabled:
        return None

    app = _create_app()

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


def heartbeat(note: str = "") -> None:
    """Optional periodic ping so the page shows activity even without orders."""
    record_event("heartbeat", note=note or "running")
