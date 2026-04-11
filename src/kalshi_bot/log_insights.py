"""Parse recent structured JSONL and derive session feedback for the LLM (not ML training).

Research notes (heuristic, not financial advice):
- Binary EV is q - p (true prob minus price); Kalshi taker/winner fees eat thin edges—many public writeups
  target fee-adjusted edge (often a few ¢ on $1) before counting an entry as positive EV.
- Calibration varies by domain/horizon (e.g. academic work on Kalshi/Polymarket calibration); treat market
  prices as noisy signals, not guaranteed probabilities.
- Sample size: very small W–L is dominated by variance; adaptation here is a conservative guardrail only.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def aggregate_structured_log_tail(
    path: Path,
    *,
    max_lines: int = 12_000,
    max_bytes: int = 6_000_000,
) -> dict[str, Any]:
    """Read the tail of the JSONL file and count ``event`` fields (best-effort).

    Returns counts suitable for dashboard / summaries; skips malformed lines.
    """
    out: dict[str, Any] = {
        "log_path": str(path),
        "lines_parsed": 0,
        "lines_skipped": 0,
        "bytes_read": 0,
        "event_counts": {},
        "top_events": [],
    }
    if not path.exists():
        out["error"] = "file_missing"
        return out
    try:
        raw = path.read_bytes()
    except OSError as exc:
        out["error"] = str(exc)
        return out
    out["bytes_read"] = len(raw)
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    lines = lines[-max_lines:]
    events: Counter[str] = Counter()
    blocked_reasons: Counter[str] = Counter()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            out["lines_skipped"] = int(out["lines_skipped"]) + 1
            continue
        if not isinstance(obj, dict):
            out["lines_skipped"] = int(out["lines_skipped"]) + 1
            continue
        ev = obj.get("event")
        if isinstance(ev, str):
            events[ev] += 1
            out["lines_parsed"] = int(out["lines_parsed"]) + 1
            if ev == "order_blocked":
                pl = obj.get("payload")
                if isinstance(pl, dict):
                    r = pl.get("reason")
                    if isinstance(r, str) and r:
                        blocked_reasons[r] += 1
        else:
            out["lines_skipped"] = int(out["lines_skipped"]) + 1
    out["event_counts"] = dict(events.most_common(40))
    out["top_events"] = [f"{k}: {v}" for k, v in events.most_common(12)]
    if blocked_reasons:
        out["order_blocked_reasons"] = dict(blocked_reasons.most_common(10))
    return out


def adaptive_edge_deltas_from_wl(
    snapshot: dict[str, int],
    *,
    enabled: bool,
    min_closed: int = 5,
) -> tuple[float, float, bool, str]:
    """Return extra (min_net_edge, mid_extra_edge) from session W–L, stress flag, and prompt note.

    When the bot has enough **closed** trades and losses dominate, tighten deterministic gates slightly.
    This does not train the model; it nudges thresholds and the prompt toward selectivity.
    """
    if not enabled:
        return (0.0, 0.0, False, "")
    w = int(snapshot.get("wins", 0))
    l = int(snapshot.get("losses", 0))
    t = int(snapshot.get("ties", 0))
    closed = w + l
    if closed < max(1, min_closed):
        return (0.0, 0.0, False, "")
    loss_rate = l / closed
    if loss_rate <= 0.48:
        return (0.0, 0.0, False, "")
    stress = min(1.0, max(0.0, (loss_rate - 0.48) / 0.45))
    extra_min = 0.002 + stress * 0.035
    extra_mid = stress * 0.015
    note = (
        f"Session auto-sell tally (estimated P/L vs entry): W{w}-L{l}"
        f"{f', BE {t}' if t else ''}. "
        f"Approx. loss share among closed exits: {loss_rate:.0%}. "
        "Be extra selective: set approve=false unless fair_yes meaningfully exceeds the ask after fees; "
        "use contracts=1 unless edge is very clear."
    )
    return (extra_min, extra_mid, True, note)
