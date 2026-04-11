"""Load RSA credentials and Kalshi request signing."""

from __future__ import annotations

import os
from pathlib import Path

from kalshi_python_sync.auth import KalshiAuth


class AuthError(RuntimeError):
    """Raised when API keys cannot be loaded."""


def load_private_key_pem(*, key_path: str | None, key_pem: str | None) -> str:
    """Resolve PEM material from env: file path takes precedence over inline PEM."""
    if key_path:
        p = Path(os.path.expanduser(key_path))
        if not p.is_file():
            raise AuthError(f"KALSHI_PRIVATE_KEY_PATH not found: {p}")
        return p.read_text(encoding="utf-8")
    if key_pem:
        # Allow literal \n in env strings
        return key_pem.replace("\\n", "\n")
    raise AuthError("Set KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM")


def build_kalshi_auth(api_key_id: str, *, key_path: str | None, key_pem: str | None) -> KalshiAuth:
    """Construct SDK-compatible KalshiAuth from environment-backed settings."""
    if not api_key_id.strip():
        raise AuthError("KALSHI_API_KEY_ID is required")
    pem = load_private_key_pem(key_path=key_path, key_pem=key_pem)
    return KalshiAuth(api_key_id.strip(), pem)


def websocket_handshake_headers(auth: KalshiAuth) -> dict[str, str]:
    """Headers for GET /trade-api/ws/v2 (see Kalshi WebSocket docs)."""
    h = auth.create_auth_headers("GET", "/trade-api/ws/v2")
    h["Content-Type"] = "application/json"
    return h
