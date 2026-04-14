"""SQLite store for confirmed Kalshi bets (entries + outcomes) for master-bot learning and win-rate gates."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_bot.config import Settings, project_root

_LOCK = threading.Lock()


def _db_path(settings: Settings) -> Path:
    raw = (getattr(settings, "trade_master_db_path", None) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return project_root() / "data" / "master_bets.sqlite"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS confirmed_bets (
            id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_yes_cents INTEGER NOT NULL,
            contracts INTEGER NOT NULL,
            net_edge REAL,
            source TEXT,
            entry_ts TEXT NOT NULL,
            exit_ts TEXT,
            outcome TEXT,
            pnl_cents REAL,
            exit_reason TEXT,
            raw_payload TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_confirmed_bets_ticker ON confirmed_bets (ticker)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_confirmed_bets_exit_ts ON confirmed_bets (exit_ts)"
    )
    conn.commit()


@dataclass
class RollingStats:
    closed: int
    wins: int
    losses: int
    win_rate: float | None


def rolling_win_rate(
    settings: Settings,
    *,
    window: int,
) -> RollingStats:
    """Win rate over the last ``window`` **closed** bets (win or loss only)."""
    path = _db_path(settings)
    if not path.is_file():
        return RollingStats(closed=0, wins=0, losses=0, win_rate=None)
    with _LOCK:
        conn = _connect(path)
        try:
            ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT outcome FROM confirmed_bets
                WHERE outcome IN ('win', 'loss') AND exit_ts IS NOT NULL
                ORDER BY exit_ts DESC
                LIMIT ?
                """,
                (max(1, int(window)),),
            ).fetchall()
        finally:
            conn.close()
    wins = sum(1 for r in rows if r["outcome"] == "win")
    losses = sum(1 for r in rows if r["outcome"] == "loss")
    closed = wins + losses
    if closed == 0:
        return RollingStats(closed=0, wins=0, losses=0, win_rate=None)
    return RollingStats(closed=closed, wins=wins, losses=losses, win_rate=wins / closed)


def count_closed(settings: Settings) -> int:
    path = _db_path(settings)
    if not path.is_file():
        return 0
    with _LOCK:
        conn = _connect(path)
        try:
            ensure_schema(conn)
            n = conn.execute(
                "SELECT COUNT(*) FROM confirmed_bets WHERE exit_ts IS NOT NULL AND outcome IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()
    return int(n)


def insert_open_bet(
    settings: Settings,
    *,
    ticker: str,
    side: str,
    entry_yes_cents: int,
    contracts: int,
    net_edge: float | None,
    source: str,
    extra: dict[str, Any] | None = None,
) -> str:
    """Record a submitted buy. Returns bet id."""
    bet_id = str(uuid.uuid4())
    path = _db_path(settings)
    now = datetime.now(UTC).isoformat()
    payload = json.dumps(extra or {}, default=str)
    with _LOCK:
        conn = _connect(path)
        try:
            ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO confirmed_bets (
                    id, ticker, side, entry_yes_cents, contracts, net_edge, source,
                    entry_ts, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (bet_id, ticker, side, entry_yes_cents, contracts, net_edge, source, now, payload),
            )
            conn.commit()
        finally:
            conn.close()
    return bet_id


def close_bet_for_ticker(
    settings: Settings,
    *,
    ticker: str,
    outcome: str,
    pnl_cents: float | None,
    exit_reason: str | None = None,
) -> None:
    """Attach outcome to the most recent open entry for this ticker (best-effort)."""
    path = _db_path(settings)
    if not path.is_file():
        return
    now = datetime.now(UTC).isoformat()
    with _LOCK:
        conn = _connect(path)
        try:
            ensure_schema(conn)
            row = conn.execute(
                """
                SELECT id FROM confirmed_bets
                WHERE ticker = ? AND exit_ts IS NULL
                ORDER BY entry_ts DESC
                LIMIT 1
                """,
                (ticker,),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                """
                UPDATE confirmed_bets
                SET exit_ts = ?, outcome = ?, pnl_cents = ?, exit_reason = ?
                WHERE id = ?
                """,
                (now, outcome, pnl_cents, exit_reason, row["id"]),
            )
            conn.commit()
        finally:
            conn.close()


def export_summary(settings: Settings) -> dict[str, Any]:
    """Lightweight stats for CLI / dashboard."""
    rs = rolling_win_rate(settings, window=max(10, int(getattr(settings, "trade_master_rolling_window", 50))))
    return {
        "db_path": str(_db_path(settings)),
        "rolling_closed": rs.closed,
        "rolling_wins": rs.wins,
        "rolling_losses": rs.losses,
        "rolling_win_rate": rs.win_rate,
        "total_closed": count_closed(settings),
    }
