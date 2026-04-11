"""Pre-trade risk checks: exposure, drawdown, streak cooldown, kill switch, anti-martingale."""

from __future__ import annotations

import time
from dataclasses import dataclass

from kalshi_bot.config import Settings


@dataclass
class RiskDecision:
    """Result of a risk gate."""

    allowed: bool
    reason: str


@dataclass
class RiskState:
    """Mutable session state for drawdown, streaks, and cooldown tracking."""

    session_start_balance_cents: int | None = None
    last_balance_cents: int | None = None
    cooldown_until: float = 0.0
    consecutive_losses: int = 0
    last_order_contracts: int | None = None
    # When True, last closed trade was a loss (for anti-martingale sizing)
    last_realized_loss: bool = False


class RiskManager:
    """Enforces configured limits before any order is sent."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self.state = RiskState()

    def kill_switch_active(self) -> bool:
        return self._s.kill_switch

    def in_cooldown(self) -> bool:
        return time.time() < self.state.cooldown_until

    def record_balance_sample(self, balance_cents: int | None) -> None:
        """Track session PnL vs starting balance; trigger cooldown on drawdown steps."""
        if balance_cents is None:
            return
        if self.state.session_start_balance_cents is None:
            self.state.session_start_balance_cents = balance_cents
            self.state.last_balance_cents = balance_cents
            return

        prev = self.state.last_balance_cents or balance_cents
        self.state.last_balance_cents = balance_cents

        dd_cents = (self.state.session_start_balance_cents or balance_cents) - balance_cents
        if dd_cents > int(self._s.max_daily_drawdown_usd * 100):
            return

        loss_step_cents = prev - balance_cents
        if loss_step_cents >= 1 and self._s.cooldown_after_loss_seconds > 0:
            self.state.cooldown_until = time.time() + self._s.cooldown_after_loss_seconds

    def record_closed_trade(self, pnl_cents: float) -> None:
        """Update loss streaks; extend cooldown after streak threshold."""
        if pnl_cents < 0:
            self.state.consecutive_losses += 1
            self.state.last_realized_loss = True
        else:
            self.state.consecutive_losses = 0
            self.state.last_realized_loss = False

        if self.state.consecutive_losses >= self._s.loss_streak_threshold and self._s.cooldown_after_loss_streak_seconds > 0:
            self.state.cooldown_until = max(
                self.state.cooldown_until,
                time.time() + self._s.cooldown_after_loss_streak_seconds,
            )

    def daily_loss_usd(self) -> float:
        if self.state.session_start_balance_cents is None or self.state.last_balance_cents is None:
            return 0.0
        dd = self.state.session_start_balance_cents - self.state.last_balance_cents
        return max(0.0, dd / 100.0)

    def check_new_order(
        self,
        *,
        market_ticker: str,
        order_contracts: int,
        position_contracts_for_market: float,
        resting_orders_on_market: int,
        current_total_exposure_cents: float = 0.0,
        additional_order_exposure_cents: float = 0.0,
    ) -> RiskDecision:
        if self.kill_switch_active():
            return RiskDecision(False, "kill_switch_enabled")

        if self.in_cooldown():
            return RiskDecision(False, "cooldown")

        if self.daily_loss_usd() >= self._s.max_daily_drawdown_usd:
            return RiskDecision(False, "max_daily_drawdown_exceeded")

        projected = position_contracts_for_market + order_contracts
        if projected > self._s.max_contracts_per_market:
            return RiskDecision(False, "max_contracts_per_market_exceeded")

        if resting_orders_on_market >= self._s.max_open_orders_per_market:
            return RiskDecision(False, "max_open_orders_per_market")

        if (
            current_total_exposure_cents + additional_order_exposure_cents
            > self._s.max_exposure_cents
        ):
            return RiskDecision(False, "max_exposure_exceeded")

        # Anti-martingale: do not increase size after a loss while in loss state
        if self._s.no_martingale and self.state.last_realized_loss and self.state.last_order_contracts is not None:
            if order_contracts > self.state.last_order_contracts:
                return RiskDecision(False, "no_martingale_increase_after_loss")

        return RiskDecision(True, "ok")

    def record_order_submitted(self, order_contracts: int) -> None:
        """Call after a live/paper order is accepted so sizing rules can reference it."""
        self.state.last_order_contracts = order_contracts
