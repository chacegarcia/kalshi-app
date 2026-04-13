"""Optional Azure SQL persistence for the Python bot.

Activated only when ``SQL_CONNECTION_STRING`` is set in the environment / .env.
All public functions are fail-soft: exceptions are caught and logged at DEBUG
level so a broken DB connection never crashes the bot.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

_LOG = logging.getLogger(__name__)
_lock = threading.Lock()
_conn: Any = None

_DDL = [
    # python_bets table
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'python_bets')
    CREATE TABLE python_bets (
        id                BIGINT IDENTITY(1,1) PRIMARY KEY,
        created_at        DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        ticker            NVARCHAR(120) NOT NULL,
        side              NVARCHAR(10)  NOT NULL,
        action            NVARCHAR(10)  NOT NULL,
        count             INT           NOT NULL,
        yes_price_cents   INT           NOT NULL,
        status            NVARCHAR(20)  NOT NULL,
        order_id          NVARCHAR(120) NULL,
        client_order_id   NVARCHAR(120) NULL,
        error_message     NVARCHAR(500) NULL,
        market_title      NVARCHAR(300) NULL,
        kalshi_env        NVARCHAR(20)  NOT NULL DEFAULT 'demo',
        dry_run           BIT           NOT NULL DEFAULT 1
    )
    """,
    # index on ticker
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'IX_python_bets_ticker' AND object_id = OBJECT_ID('python_bets')
    )
    CREATE INDEX IX_python_bets_ticker ON python_bets (ticker)
    """,
    # index on created_at
    """
    IF NOT EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'IX_python_bets_created_at' AND object_id = OBJECT_ID('python_bets')
    )
    CREATE INDEX IX_python_bets_created_at ON python_bets (created_at DESC)
    """,
]


def _get_connection(connection_string: str) -> Any:
    """Return the module-level shared connection, reconnecting if needed."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.execute("SELECT 1")
                return _conn
            except Exception:  # noqa: BLE001
                try:
                    _conn.close()
                except Exception:  # noqa: BLE001
                    pass
                _conn = None

        import pyodbc  # type: ignore[import-untyped]

        _conn = pyodbc.connect(connection_string, timeout=10)
        _conn.autocommit = True
        return _conn


def ensure_schema(connection_string: str) -> None:
    """Create tables and indexes if they do not already exist (idempotent)."""
    try:
        conn = _get_connection(connection_string)
        with _lock:
            cur = conn.cursor()
            for ddl in _DDL:
                cur.execute(ddl)
            cur.close()
        _LOG.info("db_schema_ready table=python_bets")
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("db_ensure_schema_failed: %s", exc)


def insert_bet(
    connection_string: str,
    *,
    ticker: str,
    side: str,
    action: str,
    count: int,
    yes_price_cents: int,
    status: str,
    order_id: str | None = None,
    client_order_id: str | None = None,
    error_message: str | None = None,
    market_title: str | None = None,
    kalshi_env: str = "demo",
    dry_run: bool = True,
) -> None:
    """Insert one bet row into ``python_bets`` (fail-soft)."""
    if error_message and len(error_message) > 500:
        error_message = error_message[:500]
    if market_title and len(market_title) > 300:
        market_title = market_title[:300]
    try:
        conn = _get_connection(connection_string)
        with _lock:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO python_bets
                    (created_at, ticker, side, action, count, yes_price_cents,
                     status, order_id, client_order_id, error_message,
                     market_title, kalshi_env, dry_run)
                VALUES
                    (SYSUTCDATETIME(), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ticker,
                side,
                action,
                count,
                yes_price_cents,
                status,
                order_id,
                client_order_id,
                error_message,
                market_title,
                kalshi_env,
                1 if dry_run else 0,
            )
            cur.close()
        _LOG.debug("db_insert_bet ticker=%s status=%s", ticker, status)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("db_insert_bet_failed ticker=%s: %s", ticker, exc)


def get_bets(connection_string: str, limit: int = 200) -> list[dict[str, Any]]:
    """Return up to *limit* bets newest-first as plain dicts (fail-soft → empty list)."""
    try:
        conn = _get_connection(connection_string)
        with _lock:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT TOP {int(limit)}
                    id, created_at, ticker, side, action, count,
                    yes_price_cents, status, order_id, client_order_id,
                    error_message, market_title, kalshi_env, dry_run
                FROM python_bets
                ORDER BY created_at DESC
                """,  # noqa: S608
            )
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
            cur.close()
        result = []
        for row in rows:
            d: dict[str, Any] = dict(zip(cols, row))
            # Convert datetime to ISO string for JSON serialisation
            if isinstance(d.get("created_at"), datetime):
                d["created_at"] = d["created_at"].replace(tzinfo=timezone.utc).isoformat()
            # Convert bit → bool
            if "dry_run" in d:
                d["dry_run"] = bool(d["dry_run"])
            result.append(d)
        return result
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("db_get_bets_failed: %s", exc)
        return []
