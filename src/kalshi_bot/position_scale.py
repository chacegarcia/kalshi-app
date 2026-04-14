"""Per-ticker probe → scale-up / partial avg-down / recovery-exit state (SQLite).

**Intended flow:** start with a small probe (see ``TRADE_SCALE_PROBE_CONTRACTS`` in execution); when the
position **marks up**, add contracts (pyramid); when it **marks down**, add a **small** clip and start a
recovery window — if bid does not recover before the deadline, flatten. Take-profit / stop still run via
``auto_sell`` (``TRADE_EXIT_*``).

Educational wiring only — not investment advice.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from kalshi_bot.client import KalshiSdkClient
from kalshi_bot.config import Settings, project_root
from kalshi_bot.execution import DryRunLedger
from kalshi_bot.logger import StructuredLogger
from kalshi_bot.market_data import best_yes_bid_cents, get_orderbook, lift_yes_ask_cents_from_orderbook
from kalshi_bot.portfolio import fetch_portfolio_snapshot
from kalshi_bot.risk import RiskManager
from kalshi_bot.strategy import should_skip_buy_ticker_substrings, skip_buy_yes_longshot
from kalshi_bot.trading import make_limit_intent, trade_execute

_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS position_scale_state (
    ticker TEXT PRIMARY KEY,
    scale_up_steps INTEGER NOT NULL DEFAULT 0,
    avg_down_rounds INTEGER NOT NULL DEFAULT 0,
    recovery_deadline_ts REAL,
    last_action_ts REAL NOT NULL DEFAULT 0
);
"""


def _db_path(settings: Settings) -> Path:
    raw = (getattr(settings, "trade_scale_state_db_path", None) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return project_root() / "data" / "position_scale.sqlite"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


class ScaleStateRow:
    __slots__ = ("scale_up_steps", "avg_down_rounds", "recovery_deadline_ts", "last_action_ts")

    def __init__(
        self,
        *,
        scale_up_steps: int,
        avg_down_rounds: int,
        recovery_deadline_ts: float | None,
        last_action_ts: float,
    ) -> None:
        self.scale_up_steps = scale_up_steps
        self.avg_down_rounds = avg_down_rounds
        self.recovery_deadline_ts = recovery_deadline_ts
        self.last_action_ts = last_action_ts


def load_state(settings: Settings, ticker: str) -> ScaleStateRow:
    path = _db_path(settings)
    if not path.is_file():
        return ScaleStateRow(
            scale_up_steps=0, avg_down_rounds=0, recovery_deadline_ts=None, last_action_ts=0.0
        )
    with _LOCK:
        conn = _connect(path)
        try:
            ensure_schema(conn)
            row = conn.execute(
                "SELECT scale_up_steps, avg_down_rounds, recovery_deadline_ts, last_action_ts "
                "FROM position_scale_state WHERE ticker = ?",
                (ticker,),
            ).fetchone()
        finally:
            conn.close()
    if row is None:
        return ScaleStateRow(
            scale_up_steps=0, avg_down_rounds=0, recovery_deadline_ts=None, last_action_ts=0.0
        )
    rd = row["recovery_deadline_ts"]
    return ScaleStateRow(
        scale_up_steps=int(row["scale_up_steps"]),
        avg_down_rounds=int(row["avg_down_rounds"]),
        recovery_deadline_ts=float(rd) if rd is not None else None,
        last_action_ts=float(row["last_action_ts"] or 0.0),
    )


def save_state(settings: Settings, ticker: str, state: ScaleStateRow) -> None:
    path = _db_path(settings)
    with _LOCK:
        conn = _connect(path)
        try:
            ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO position_scale_state (
                    ticker, scale_up_steps, avg_down_rounds, recovery_deadline_ts, last_action_ts
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    scale_up_steps = excluded.scale_up_steps,
                    avg_down_rounds = excluded.avg_down_rounds,
                    recovery_deadline_ts = excluded.recovery_deadline_ts,
                    last_action_ts = excluded.last_action_ts
                """,
                (
                    ticker,
                    state.scale_up_steps,
                    state.avg_down_rounds,
                    state.recovery_deadline_ts,
                    state.last_action_ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def clear_state(settings: Settings, ticker: str) -> None:
    path = _db_path(settings)
    if not path.is_file():
        return
    with _LOCK:
        conn = _connect(path)
        try:
            ensure_schema(conn)
            conn.execute("DELETE FROM position_scale_state WHERE ticker = ?", (ticker,))
            conn.commit()
        finally:
            conn.close()


def _cooldown_ok(settings: Settings, last_ts: float) -> bool:
    cd = float(settings.trade_scale_cooldown_seconds)
    if cd <= 0:
        return True
    return (time.time() - last_ts) >= cd


def _liquidate_long_yes(
    client: KalshiSdkClient,
    settings: Settings,
    risk: RiskManager,
    ledger: DryRunLedger | None,
    log: StructuredLogger,
    ticker: str,
    *,
    reason: str,
) -> bool:
    snap = fetch_portfolio_snapshot(client, ticker=ticker)
    signed = float(snap.positions_by_ticker.get(ticker, 0.0))
    cnt = int(round(signed))
    if cnt < 1:
        return False
    ob = get_orderbook(client, ticker)
    best = best_yes_bid_cents(ob)
    if best is None:
        log.warning("position_scale_recovery_exit_no_bid", ticker=ticker)
        return False
    limit_cents = max(1, int(best) - int(settings.trade_exit_sell_aggression_cents))
    tif = settings.trade_exit_sell_time_in_force
    intent = make_limit_intent(
        ticker=ticker,
        side="yes",
        action="sell",
        count=cnt,
        yes_price_cents=limit_cents,
        time_in_force=tif,
    )
    log.info(
        "position_scale_recovery_exit",
        ticker=ticker,
        reason=reason,
        count=cnt,
        limit_yes_price_cents=limit_cents,
        best_yes_bid_cents=best,
    )
    trade_execute(client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger)
    return True


def run_position_scale_tick(
    client: KalshiSdkClient,
    settings: Settings,
    risk: RiskManager,
    ledger: DryRunLedger | None,
    log: StructuredLogger,
    ticker: str,
    *,
    entry_yes_cents: int | None,
) -> tuple[str, str | None]:
    """One decision for ``ticker``: recovery exit, scale-up buy, or avg-down buy.

    Returns ``(tag, detail)`` where tag is ``noop`` | ``sold`` | ``bought_scale`` | ``bought_avg_down`` | ``skip``.
    """
    if not settings.trade_scale_manage_enabled:
        return "noop", None
    if entry_yes_cents is None or not (1 <= entry_yes_cents <= 99):
        return "skip", "no_entry_reference"

    snap = fetch_portfolio_snapshot(client, ticker=ticker)
    held = float(snap.positions_by_ticker.get(ticker, 0.0))
    if held <= 0:
        clear_state(settings, ticker)
        return "noop", None

    ob = get_orderbook(client, ticker)
    best = best_yes_bid_cents(ob)
    lift = lift_yes_ask_cents_from_orderbook(ob)
    if best is None or lift is None:
        return "skip", "no_book"

    st = load_state(settings, ticker)
    now = time.time()
    mark = float(best) - float(entry_yes_cents)
    clear_mark = float(settings.trade_scale_recovery_clear_min_mark_vs_entry_cents)

    if st.recovery_deadline_ts is not None:
        if mark >= clear_mark:
            st.recovery_deadline_ts = None
            save_state(settings, ticker, st)
            log.info(
                "position_scale_recovery_cleared",
                ticker=ticker,
                mark_vs_entry_cents=mark,
                clear_threshold_cents=clear_mark,
            )
        elif now >= float(st.recovery_deadline_ts):
            fired = _liquidate_long_yes(
                client,
                settings,
                risk,
                ledger,
                log,
                ticker,
                reason="recovery_deadline_expired",
            )
            clear_state(settings, ticker)
            return ("sold", "recovery_exit_deadline") if fired else ("skip", "recovery_exit_failed")
        else:
            return "noop", "recovery_watch"

    if not _cooldown_ok(settings, st.last_action_ts):
        return "noop", None

    contracts = int(round(held))
    max_pos = int(settings.trade_scale_max_position_contracts)
    room = max(0, max_pos - contracts)

    # --- Scale up (winner): add when mark is sufficiently positive ---
    if settings.trade_scale_up_enabled and st.recovery_deadline_ts is None:
        need = float(settings.trade_scale_up_min_mark_vs_entry_cents)
        if mark >= need and st.scale_up_steps < int(settings.trade_scale_up_max_steps):
            add = int(settings.trade_scale_up_add_contracts)
            add = max(1, min(add, room))
            if add >= 1 and lift <= int(round(settings.trade_entry_effective_max_yes_ask_dollars * 100.0)):
                if not should_skip_buy_ticker_substrings(settings, ticker) and not skip_buy_yes_longshot(
                    settings, int(lift)
                ):
                    lim = max(1, min(99, int(lift)))
                    intent = make_limit_intent(
                        ticker=ticker,
                        side="yes",
                        action="buy",
                        count=add,
                        yes_price_cents=lim,
                        double_down=False,
                        position_scale_addon=True,
                        master_net_edge=None,
                        master_source="position_scale_up",
                    )
                    log.info(
                        "position_scale_up",
                        ticker=ticker,
                        count=add,
                        yes_price_cents=lim,
                        mark_vs_entry_cents=mark,
                        step=st.scale_up_steps + 1,
                    )
                    trade_execute(
                        client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger
                    )
                    st.scale_up_steps += 1
                    st.last_action_ts = now
                    save_state(settings, ticker, st)
                    return "bought_scale", f"+{add} @ {lim}¢"

    # --- Avg down (loser): small add + recovery watch ---
    if settings.trade_scale_avg_down_enabled and room >= 1:
        loss_need = float(settings.trade_scale_avg_down_min_loss_mark_cents)
        if mark <= -loss_need and st.avg_down_rounds < int(settings.trade_scale_avg_down_max_rounds):
            add = int(settings.trade_scale_avg_down_contracts)
            add = max(1, min(add, room))
            if not should_skip_buy_ticker_substrings(settings, ticker) and not skip_buy_yes_longshot(
                settings, int(lift)
            ):
                lim = max(1, min(99, int(lift)))
                intent = make_limit_intent(
                    ticker=ticker,
                    side="yes",
                    action="buy",
                    count=add,
                    yes_price_cents=lim,
                    double_down=False,
                    position_scale_addon=True,
                    master_net_edge=None,
                    master_source="position_scale_avg_down",
                )
                wait = float(settings.trade_scale_recovery_wait_seconds)
                log.info(
                    "position_scale_avg_down",
                    ticker=ticker,
                    count=add,
                    yes_price_cents=lim,
                    mark_vs_entry_cents=mark,
                    recovery_deadline_ts=now + max(1.0, wait),
                    round=st.avg_down_rounds + 1,
                )
                trade_execute(
                    client=client, settings=settings, risk=risk, log=log, intent=intent, ledger=ledger
                )
                st.avg_down_rounds += 1
                st.recovery_deadline_ts = now + max(1.0, wait)
                st.last_action_ts = time.time()
                save_state(settings, ticker, st)
                return "bought_avg_down", f"+{add} avg-down @ {lim}¢"

    return "noop", None
