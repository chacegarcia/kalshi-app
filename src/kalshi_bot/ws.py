"""Async WebSocket client for Kalshi market data (ticker + orderbook).

This is the live streaming entrypoint. For research, pair with `paper_engine` or
record messages to JSONL and feed them to `backtest.load_price_records`.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

from kalshi_python_sync.auth import KalshiAuth

from kalshi_bot.auth import websocket_handshake_headers

OnMessage = Callable[[dict[str, Any]], Awaitable[None]]


class KalshiWS:
    """Authenticated WS with reconnect + exponential backoff."""

    def __init__(
        self,
        *,
        ws_url: str,
        auth: KalshiAuth,
        on_message: OnMessage,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        self._ws_url = ws_url
        self._auth = auth
        self._on_message = on_message
        self._max_backoff = max_backoff_seconds
        self._msg_id = 1

    def _next_id(self) -> int:
        i = self._msg_id
        self._msg_id += 1
        return i

    async def subscribe_ticker(self, ws: WebSocketClientProtocol) -> None:
        sub = {"id": self._next_id(), "cmd": "subscribe", "params": {"channels": ["ticker"]}}
        await ws.send(json.dumps(sub))

    async def subscribe_orderbook(self, ws: WebSocketClientProtocol, tickers: list[str]) -> None:
        sub = {
            "id": self._next_id(),
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": tickers},
        }
        await ws.send(json.dumps(sub))

    async def run(self, *, market_tickers: list[str]) -> None:
        """Loop forever: connect, subscribe, process, reconnect on failure."""
        attempt = 0
        while True:
            headers = websocket_handshake_headers(self._auth)
            try:
                async with websockets.connect(
                    self._ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    attempt = 0
                    await self.subscribe_ticker(ws)
                    if market_tickers:
                        await self.subscribe_orderbook(ws, market_tickers)
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        await self._on_message(data)
            except Exception:
                attempt += 1
                delay = min(self._max_backoff, (2 ** min(attempt, 8)) * 0.25)
                delay += random.random() * 0.5
                await asyncio.sleep(delay)


# Backward-compatible alias
KalshiWebSocketClient = KalshiWS
