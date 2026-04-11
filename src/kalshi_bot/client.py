"""Thin wrapper around the official Kalshi sync SDK with retries."""

from __future__ import annotations

from typing import Callable, TypeVar

from kalshi_python_sync import (
    ApiClient,
    Configuration,
    KalshiAuth,
    MarketApi,
    OrdersApi,
    PortfolioApi,
)
from kalshi_python_sync.exceptions import ApiException
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

T = TypeVar("T")


def _build_api_client(host: str, auth: KalshiAuth) -> ApiClient:
    configuration = Configuration(host=host)
    client = ApiClient(configuration=configuration)
    client.kalshi_auth = auth
    return client


class KalshiSdkClient:
    """Owns ApiClient and typed API facades."""

    def __init__(self, *, rest_base_url: str, auth: KalshiAuth) -> None:
        self._api_client = _build_api_client(rest_base_url, auth)
        self.markets = MarketApi(self._api_client)
        self.orders = OrdersApi(self._api_client)
        self.portfolio = PortfolioApi(self._api_client)


def with_rest_retry(fn: Callable[..., T]) -> Callable[..., T]:
    """Retry transient network / 5xx failures with bounded backoff."""
    return retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential_jitter(initial=0.5, max=20),
        retry=retry_if_exception_type(
            (
                ApiException,
                ConnectionError,
                TimeoutError,
                OSError,
            )
        ),
    )(fn)

