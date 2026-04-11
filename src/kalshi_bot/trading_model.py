r"""Trading-app semantics on Kalshi binary YES markets.

Kalshi exposes **contracts** in the API; this codebase treats each YES contract as one **share**
of a $1 max-payoff claim (settlement $1 if YES, $0 if NO).

- **Share price**: implied YES probability, expressed as dollars 0–1 on the $1 face (UI: **cents**,
  e.g. 42¢ ≈ 42% implied). Moves in that price are the natural analogue of **stock price movement**
  (probability revises as information arrives).
- **Position**: long YES **shares** = contracts held; short side uses the NO book symmetrically.
- **Notional** at a limit: approximately ``shares × price_cents`` in cents of cash at risk to hold
  the position (before fees).
- **Gross P/L on exit** (ignoring fees): ``shares × (exit_price_cents − entry_price_cents)`` when
  both prices are in the same cents space.

Fee-aware edge in ``edge_math`` is already computed in dollars per **unit** (per share) after
averaging taker fees for the order size. Strategy gates (min edge, mid penalty) anticipate
adverse movement near 50¢ where fees bite hardest—same as being cautious in a “choppy” name.
"""

from __future__ import annotations


def yes_position_notional_cents(*, shares: int, yes_price_cents: int) -> int:
    """Approximate cash tied up at the limit: ``shares × price_cents`` (cents)."""
    s = max(0, int(shares))
    p = max(1, min(99, int(yes_price_cents)))
    return s * p


def gross_pnl_cents_from_price_move(
    *,
    shares: int,
    exit_price_cents: int,
    entry_price_cents: int,
) -> int:
    """Gross P/L in cents vs a known per-share entry (before fees): ``shares × (exit − entry)``."""
    s = max(0, int(shares))
    return s * (int(exit_price_cents) - int(entry_price_cents))
