"""Order placement, cancellation, dry-run simulation, stale-order sweeps."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from kalshi_python_sync.models.order import Order

from kalshi_bot.client import KalshiSdkClient, with_rest_retry
from kalshi_bot.config import Settings
from kalshi_bot.logger import StructuredLogger
from kalshi_bot.monitor import record_event
from kalshi_bot.portfolio import fetch_portfolio_snapshot
from kalshi_bot.risk import RiskManager
from kalshi_bot.sizing import (
    adjust_buy_yes_count_for_notional_floor,
    cap_buy_yes_count_for_notional,
    effective_max_contracts,
    effective_max_exposure_cents,
    next_buy_yes_notional_min_max,
    parse_notional_sweep_usd,
)
from kalshi_bot.strategy import TradeIntent, projected_abs_position_after


@dataclass
class SimulatedOrder:
    client_order_id: str
    ticker: str
    created_ts: float


@dataclass
class DryRunLedger:
    """In-memory standing for paper mode (not exchange-backed)."""

    orders: list[SimulatedOrder] = field(default_factory=list)

    def record_intent(self, intent: TradeIntent) -> SimulatedOrder:
        sim = SimulatedOrder(
            client_order_id=str(uuid.uuid4()),
            ticker=intent.ticker,
            created_ts=time.time(),
        )
        self.orders.append(sim)
        return sim


def _warn_live_banner(settings: Settings) -> None:
    print(
        "\n*** WARNING: LIVE order submission enabled "
        f"(LIVE_TRADING=true, DRY_RUN=false, env={settings.kalshi_env}). "
        "Verify limits and market ticker before continuing.\n"
    )


@with_rest_retry
def cancel_all_resting_orders(client: KalshiSdkClient, log: StructuredLogger) -> int:
    """Cancel all resting orders (paginated + batch)."""
    cursor: str | None = None
    ids: list[str] = []
    while True:
        resp = client.orders.get_orders(status="resting", limit=200, cursor=cursor)
        batch: list[Order] = list(getattr(resp, "orders", []) or [])
        ids.extend(o.order_id for o in batch)
        cursor = getattr(resp, "cursor", None)
        if not cursor or not batch:
            break

    cancelled = 0
    for i in range(0, len(ids), 20):
        chunk = ids[i : i + 20]
        if not chunk:
            break
        client.orders.batch_cancel_orders(ids=chunk)
        cancelled += len(chunk)
        log.info("batch_cancel", order_ids=chunk)
    return cancelled


@with_rest_retry
def cancel_stale_orders(
    client: KalshiSdkClient,
    settings: Settings,
    log: StructuredLogger,
) -> int:
    """Cancel resting orders older than `stale_order_seconds`."""
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.stale_order_seconds)
    cursor: str | None = None
    stale_ids: list[str] = []
    while True:
        resp = client.orders.get_orders(status="resting", limit=200, cursor=cursor)
        batch: list[Order] = list(getattr(resp, "orders", []) or [])
        for o in batch:
            ct = o.created_time
            if ct is None:
                continue
            c = ct if ct.tzinfo else ct.replace(tzinfo=UTC)
            if c < cutoff:
                stale_ids.append(o.order_id)
        cursor = getattr(resp, "cursor", None)
        if not cursor or not batch:
            break

    cancelled = 0
    for i in range(0, len(stale_ids), 20):
        chunk = stale_ids[i : i + 20]
        if not chunk:
            break
        client.orders.batch_cancel_orders(ids=chunk)
        cancelled += len(chunk)
        log.info("stale_cancel", order_ids=chunk)
    return cancelled


@with_rest_retry
def place_limit_order_live(client: KalshiSdkClient, intent: TradeIntent) -> object:
    """Submit a limit order via SDK (caller must enforce LIVE_TRADING + risk)."""
    return client.orders.create_order(
        ticker=intent.ticker,
        client_order_id=str(uuid.uuid4()),
        side=intent.side,
        action=intent.action,
        count=intent.count,
        yes_price=intent.yes_price_cents,
        time_in_force=intent.time_in_force,
    )


def execute_intent(
    *,
    client: KalshiSdkClient,
    settings: Settings,
    risk: RiskManager,
    log: StructuredLogger,
    intent: TradeIntent,
    ledger: DryRunLedger | None = None,
) -> None:
    """Risk-check then either simulate or place a real order."""
    snap = fetch_portfolio_snapshot(client, ticker=intent.ticker)
    risk.record_balance_sample(snap.balance_cents)

    min_n = settings.trade_min_order_notional_usd
    max_n = settings.trade_max_order_notional_usd
    if intent.side == "yes" and intent.action == "buy":
        min_n, max_n = next_buy_yes_notional_min_max(settings)
        if parse_notional_sweep_usd(settings.trade_notional_sweep_usd):
            log.info(
                "notional_sweep_step",
                ticker=intent.ticker,
                target_usd=min_n,
                sweep=settings.trade_notional_sweep_usd,
            )

    capped = cap_buy_yes_count_for_notional(
        intent.count,
        yes_price_cents=intent.yes_price_cents,
        max_notional_usd=max_n,
        side=intent.side,
        action=intent.action,
    )
    if capped != intent.count:
        intent = replace(intent, count=capped)

    max_c = effective_max_contracts(
        settings, balance_cents=snap.balance_cents, yes_price_cents=intent.yes_price_cents
    )
    max_exp = effective_max_exposure_cents(settings, snap.balance_cents)
    if intent.count > max_c:
        intent = replace(intent, count=max_c)

    if intent.side == "yes" and intent.action == "buy":
        floored = adjust_buy_yes_count_for_notional_floor(
            intent.count,
            yes_price_cents=intent.yes_price_cents,
            min_notional_usd=min_n,
            max_notional_usd=max_n,
            max_contracts=max_c,
        )
        if floored < 1:
            log.info(
                "order_blocked",
                reason="min_order_notional_unreachable",
                ticker=intent.ticker,
                min_usd=min_n,
                max_usd=max_n,
            )
            record_event("blocked", reason="min_order_notional_unreachable", intent=intent)
            return
        if floored != intent.count:
            intent = replace(intent, count=floored)

    if intent.count < 1:
        log.info("order_blocked", reason="zero_contracts_after_balance_sizing", ticker=intent.ticker)
        record_event("blocked", reason="zero_contracts_after_balance_sizing", intent=intent)
        return

    signed = snap.positions_by_ticker.get(intent.ticker, 0.0)
    projected_abs = projected_abs_position_after(signed, intent)
    resting = snap.resting_orders_by_ticker.get(intent.ticker, 0)
    add_exp = (
        float(intent.count * intent.yes_price_cents) if (intent.side == "yes" and intent.action == "buy") else 0.0
    )

    decision = risk.check_new_order(
        market_ticker=intent.ticker,
        order_contracts=intent.count,
        projected_abs_position=projected_abs,
        resting_orders_on_market=resting,
        current_total_exposure_cents=snap.total_exposure_cents,
        additional_order_exposure_cents=add_exp,
        order_increases_exposure=(intent.action == "buy"),
        max_contracts_override=max_c,
        max_exposure_cents_override=max_exp,
    )
    if not decision.allowed:
        log.info("order_blocked", reason=decision.reason, intent=intent)
        record_event("blocked", reason=decision.reason, intent=intent)
        return

    if settings.dry_run:
        ldg = ledger or DryRunLedger()
        sim = ldg.record_intent(intent)
        risk.record_order_submitted(intent.count)
        log.info("dry_run_order", simulated_client_order_id=sim.client_order_id, intent=intent)
        record_event(
            "dry_run",
            simulated_client_order_id=sim.client_order_id,
            ticker=intent.ticker,
            count=intent.count,
            yes_price_cents=intent.yes_price_cents,
        )
        return

    if not settings.can_send_real_orders:
        log.warning("order_refused", reason="LIVE_TRADING_false_or_misconfigured", intent=intent)
        record_event("refused", reason="LIVE_TRADING_false_or_misconfigured", intent=intent)
        return

    if settings.kalshi_env == "prod":
        _warn_live_banner(settings)

    log.info("live_order_submit", intent=intent, env=settings.kalshi_env)
    record_event("live_submit", env=settings.kalshi_env, intent=intent)
    resp = place_limit_order_live(client, intent)
    risk.record_order_submitted(intent.count)
    oid = getattr(resp, "order", None)
    order_id = getattr(oid, "order_id", None) if oid is not None else getattr(resp, "order_id", None)
    log.info("live_order_ack", response_type=type(resp).__name__)
    record_event("live_ack", response_type=type(resp).__name__, order_id=order_id)
