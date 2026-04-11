"""Portfolio snapshots: positions and resting orders from the REST API."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_python_sync.models.order import Order

from kalshi_bot.client import KalshiSdkClient, with_rest_retry


@dataclass
class PortfolioSnapshot:
    positions_by_ticker: dict[str, float]
    resting_orders_by_ticker: dict[str, int]
    balance_cents: int | None
    total_exposure_cents: float


def _position_contracts(market_positions: list[object]) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in market_positions:
        t = getattr(p, "ticker", None)
        fp = getattr(p, "position_fp", None)
        if not t or fp is None:
            continue
        try:
            out[str(t)] = float(str(fp))
        except ValueError:
            continue
    return out


def _total_exposure_cents(market_positions: list[object]) -> float:
    total = 0.0
    for p in market_positions:
        exp = getattr(p, "market_exposure_dollars", None)
        if exp is None:
            continue
        try:
            total += float(str(exp)) * 100.0
        except ValueError:
            continue
    return total


@with_rest_retry
def get_balance_cents(client: KalshiSdkClient) -> int | None:
    """Cash balance in cents (None if API omits it)."""
    bal = client.portfolio.get_balance()
    return getattr(bal, "balance", None)


@with_rest_retry
def fetch_portfolio_snapshot(client: KalshiSdkClient, *, ticker: str | None = None) -> PortfolioSnapshot:
    """Aggregate resting orders and positions. Exposure sums all markets (for risk cap)."""
    bal = client.portfolio.get_balance()
    balance_cents = getattr(bal, "balance", None)

    pos_resp = client.portfolio.get_positions(
        ticker=ticker,
        count_filter="position",
        limit=500,
    )
    mpos = list(getattr(pos_resp, "market_positions", []) or [])
    positions = _position_contracts(mpos)

    pos_all = client.portfolio.get_positions(count_filter="position", limit=1000)
    all_mpos = list(getattr(pos_all, "market_positions", []) or [])
    exposure = _total_exposure_cents(all_mpos)

    orders_cursor: str | None = None
    resting_by: dict[str, int] = {}
    while True:
        ords = client.orders.get_orders(status="resting", ticker=ticker, limit=200, cursor=orders_cursor)
        batch: list[Order] = list(getattr(ords, "orders", []) or [])
        for o in batch:
            resting_by[o.ticker] = resting_by.get(o.ticker, 0) + 1
        orders_cursor = getattr(ords, "cursor", None)
        if not orders_cursor or not batch:
            break

    return PortfolioSnapshot(
        positions_by_ticker=positions,
        resting_orders_by_ticker=resting_by,
        balance_cents=balance_cents,
        total_exposure_cents=exposure,
    )


@with_rest_retry
def get_market_position_row(client: KalshiSdkClient, ticker: str) -> object | None:
    """Return the raw ``market_positions`` row for ``ticker``, or None."""
    pos_resp = client.portfolio.get_positions(
        ticker=ticker,
        count_filter="position",
        limit=50,
    )
    for p in getattr(pos_resp, "market_positions", []) or []:
        if getattr(p, "ticker", None) == ticker:
            return p
    return None


def print_portfolio_balance_line(client: KalshiSdkClient) -> None:
    """Print cash balance and total exposure to stdout (e.g. after a trading pass summary)."""
    try:
        snap = fetch_portfolio_snapshot(client, ticker=None)
        bal = snap.balance_cents
        exp = float(snap.total_exposure_cents)
        if bal is None:
            print(f"Account: cash n/a · exposure ${exp / 100:.2f}", flush=True)
        else:
            print(f"Account: cash ${bal / 100:.2f} · exposure ${exp / 100:.2f}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Account balance: (could not fetch: {exc})", flush=True)


def estimate_yes_entry_cents_from_position(p: object) -> int | None:
    """Rough average YES entry in cents from ``total_traded_dollars`` / ``position_fp`` (not always exact cost basis)."""
    fp = getattr(p, "position_fp", None)
    tt = getattr(p, "total_traded_dollars", None)
    if fp is None or tt is None:
        return None
    try:
        contracts = abs(float(str(fp)))
        traded = float(str(tt))
    except ValueError:
        return None
    if contracts < 1e-9:
        return None
    per_contract_dollars = traded / contracts
    cents = int(round(per_contract_dollars * 100.0))
    return max(1, min(99, cents))
