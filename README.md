# Kalshi research / trading bot (Python)

Python tooling for **Kalshi REST + WebSocket** access, **paper fill simulation**, **rule backtests**, **walk-forward / OOS-style splits**, **parameter sweeps**, and **risk metrics**. This project is for **research and education** only.

**There is no guarantee of profitability.** Past simulations, backtests, or live demo results do not predict future performance.

## Features

- **API:** Official `kalshi_python_sync` client (`client.py`, `auth.py`, `config.py`).
- **Market data:** Markets + orderbooks (`market_data.py`).
- **Live streaming:** Authenticated WebSocket client (`ws.py`).
- **Live orders:** Optional real orders when `LIVE_TRADING=true` and `DRY_RUN=false` (see `execution.py`).
- **Paper engine:** Configurable fill assumptions (`paper_engine.py`) — not a replica of the exchange matcher.
- **Backtest:** JSONL snapshots, walk-forward windows, parameter grids (`backtest.py`).
- **Metrics:** Win rate, edge estimate, max drawdown, Sharpe-like ratio, fee/slippage stress (`metrics.py`).
- **Risk:** Max exposure, max contracts per market, daily drawdown, cooldowns, loss-streak cooldown, **no martingale** (block size increase after loss), kill switch (`risk.py`).

## Default command

With no subcommand, the CLI runs `**run`** (same as `kalshi-bot run`):

```bash
kalshi-bot
kalshi-bot run --dry-run
```

## Setup (local only — no GitHub required)

Use the project from **any folder on your computer** (copy, unzip, or clone — hosting is optional). Running the bot does **not** depend on GitHub, `git`, or a remote.

```bash
cd path/to/kalshi-trading-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

**Configuration:** **`.env.example`** lists every variable: **API**, **risk / session stop-loss** (`TRADE_STOP_MAX_SESSION_LOSS_USD` aliases `MAX_DAILY_DRAWDOWN_USD`), **trading** (`TRADE_MARKET_TICKER`, `TRADE_BUY_*` entry, `TRADE_TAKE_PROFIT_*` exit, pacing), **paper**, **dashboard**, **logging**. Copy to **`.env`** — the app reads **`.env`** only at runtime.

- **Your real Kalshi account:** set `KALSHI_ENV=prod` and create API keys in **[kalshi.com](https://kalshi.com/)** account settings (not the demo site).
- **Sandbox / demo only:** set `KALSHI_ENV=demo` and use keys from [demo.kalshi.co](https://demo.kalshi.co/).

### Run + monitor

`kalshi-bot run` (the default) starts a small **local Flask dashboard** (default `http://127.0.0.1:5050`) and tries to **open it in your browser** so you can watch `dry_run`, `live_submit`, `blocked`, and heartbeat rows. Disable with `run --no-web` or `DASHBOARD_ENABLED=false`.

## Module map (where to plug in research)


| File              | Role                                                                                  |
| ----------------- | ------------------------------------------------------------------------------------- |
| `config.py`       | Env-driven settings (`KALSHI_`*, risk, strategy, paper defaults).                     |
| `auth.py`         | RSA PEM loading + WebSocket signing headers.                                          |
| `client.py`       | `ApiClient` + `MarketApi` / `OrdersApi` / `PortfolioApi`.                             |
| `market_data.py`  | REST helpers for markets and order books.                                             |
| `fees.py`         | Kalshi general taker/maker fee (P×(1−P) schedule).                                   |
| `edge_math.py`    | Boxed YES+NO surplus, fee-adjusted directional edge vs fair value.                    |
| `scanner.py`      | `kalshi-bot scan` — rank boxed arb + edge columns.                                    |
| `llm_screen.py`   | OpenAI JSON verdicts (`fair_yes` / full opportunity).                                  |
| `llm_runner.py`   | **`llm-trade`** — scan markets, LLM + deterministic edge gate, optional orders.       |
| `sizing.py`       | Balance-scaled max contracts / exposure (`TRADE_RISK_PCT_OF_BALANCE_*`).              |
| `ws.py`           | Async `KalshiWS` WebSocket client.                                                    |
| `paper_engine.py` | **Fill simulation** — replace `match_limit_order` / `simulate_fill` with your model.  |
| `backtest.py`     | **Backtest engine** — swap `strategy_signal_fn` or `strategy_factory` for your rules. |
| `strategy.py`     | **Signals** — edit `signal_from_bar` / `SampleSpreadGapStrategy` or add new classes.  |
| `risk.py`         | **Risk gates** — extend `RiskManager.check_new_order` as needed.                      |
| `metrics.py`      | **Reporting** — add metrics; keep disclaimers visible.                                |
| `monitor.py`      | **Browser dashboard** (Flask) — order/risk events during `run`.                           |
| `main.py`         | CLI entry (`run`, `backtest`, `sweep`, `walk-forward`, `sensitivity`, …).             |
| `execution.py`    | Live / dry-run order placement + stale cancel helpers.                                |


## CLI

```bash
kalshi-bot                          # default: run strategy loop
kalshi-bot list-markets
kalshi-bot scan [--limit N] [--llm]
kalshi-bot llm-trade [--execute]
kalshi-bot discover-trade [--execute]
kalshi-bot watch-market TICKER
kalshi-bot place-test-order [--ticker T]
kalshi-bot cancel-all
kalshi-bot run [--dry-run|--live|--no-web]

# Research (JSONL rows: ts, ticker, yes_bid_dollars, yes_ask_dollars)
kalshi-bot backtest path/to/prices.jsonl
kalshi-bot sweep path/to/prices.jsonl
kalshi-bot walk-forward path/to/prices.jsonl
kalshi-bot sensitivity path/to/prices.jsonl
```

## JSONL format for backtests

One JSON object per line:

```json
{"ts": 1710000000.0, "ticker": "KXFOO", "yes_bid_dollars": 0.45, "yes_ask_dollars": 0.52}
```

Record your own streams from WebSocket or REST snapshots; this repo does not ship historical Kalshi downloads.

## Sample strategy

`SampleSpreadGapStrategy` (alias `SampleThresholdStrategy`) uses a **YES ask cap**, optional **minimum spread**, and optional **probability gap** away from `0.5` mid (`strategy.py`). Replace with your own logic.

## Paper engine assumptions

`PaperFillConfig` controls fill probability, partial fills, fees, and slippage. Treat outputs as **stress tests**, not exchange truth.

## Troubleshooting: `SSL: CERTIFICATE_VERIFY_FAILED` (macOS)

If HTTPS to Kalshi fails with certificate verify errors, try:

1. Reinstall deps so **`certifi`** is present — the CLI calls `apply_certifi_ca_bundle()` before any requests.
2. If you use **python.org**’s macOS installer, run **`Install Certificates.command`** in `/Applications/Python 3.x/`.
3. Corporate proxies / antivirus sometimes intercept TLS — you may need your IT root cert (not something we disable in code by default).

## References

- [Kalshi demo environment](https://docs.kalshi.com/getting_started/demo_env)
- [Python SDK](https://docs.kalshi.com/python-sdk/index)
- [WebSockets](https://docs.kalshi.com/getting_started/quick_start_websockets)

