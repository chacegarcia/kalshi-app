"""SDK client factory and trading helpers (same REST host + risk path as the live bot).

Production traffic defaults to Kalshi's unified API at ``api.elections.kalshi.com`` (all market
categories). Override with ``KALSHI_REST_BASE_URL`` / ``KALSHI_WS_URL`` in ``.env`` if needed.
"""

from __future__ import annotations

from typing import Literal

from kalshi_bot.auth import build_kalshi_auth
from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings
from kalshi_bot.execution import DryRunLedger, execute_intent
from kalshi_bot.logger import StructuredLogger
from kalshi_bot.risk import RiskManager
from kalshi_bot.strategy import TradeIntent


def build_sdk_client(settings: Settings) -> KalshiSdkClient:
    """Authenticated REST client for ``settings.rest_base_url`` (prod demo or custom override)."""
    auth = build_kalshi_auth(
        settings.kalshi_api_key_id,
        key_path=settings.kalshi_private_key_path,
        key_pem=settings.kalshi_private_key_pem,
    )
    return KalshiSdkClient(rest_base_url=settings.rest_base_url, auth=auth)


def make_limit_intent(
    *,
    ticker: str,
    side: Literal["yes", "no"],
    action: Literal["buy", "sell"],
    count: int,
    yes_price_cents: int,
    time_in_force: str = "good_till_canceled",
    double_down: bool = False,
) -> TradeIntent:
    """Build a limit order intent for ``execute_intent`` / ``trade_execute``."""
    return TradeIntent(
        ticker=ticker,
        side=side,
        action=action,
        count=count,
        yes_price_cents=yes_price_cents,
        time_in_force=time_in_force,
        double_down=double_down,
    )


def trade_execute(
    *,
    client: KalshiSdkClient,
    settings: Settings,
    risk: RiskManager,
    log: StructuredLogger,
    intent: TradeIntent,
    ledger: DryRunLedger | None = None,
) -> None:
    """Risk checks, then dry-run simulation or live ``create_order`` (same as the WebSocket bot)."""
    execute_intent(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
