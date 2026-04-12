"""In-process session controls (same process as ``kalshi-bot … --web``).

The dashboard can adjust these without restarting. Other terminals do not share this state.
"""

from __future__ import annotations

import threading
_LOCK = threading.Lock()
# Order size multiplier for **buy** orders only (1 = default; strategy still supplies base count).
_ORDER_SIZE_MULTIPLIER: int = 1

_VALID_MULT = frozenset({1, 2, 5, 10})


def get_order_size_multiplier() -> int:
    """Return 1, 2, 5, or 10. Applied to buy YES/NO contract count before risk/notional caps."""
    with _LOCK:
        m = int(_ORDER_SIZE_MULTIPLIER)
    return m if m in _VALID_MULT else 1


def set_order_size_multiplier(m: int) -> int:
    """Clamp to 1, 2, 5, or 10. Returns the value stored."""
    v = int(m)
    if v not in _VALID_MULT:
        v = 1
    with _LOCK:
        global _ORDER_SIZE_MULTIPLIER
        _ORDER_SIZE_MULTIPLIER = v
    return v
