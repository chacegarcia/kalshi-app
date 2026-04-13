"""Parse structured JSONL for per-ticker bet outcomes; optional edge penalties for future entries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kalshi_bot.config import Settings

# Lines retained when ``structured_log_preserve_executed_on_flush`` compacts the main JSONL.
STRUCTURED_LOG_BET_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "dry_run_order",
        "live_order_submit",
        "live_order_ack",
        "auto_sell_fire",
        "auto_sell_profit_estimate",
    }
)


@dataclass
class TickerOutcomeSummary:
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    unknown: int = 0


def rewrite_structured_log_keep_bet_events(
    log_path: Path,
    *,
    max_read_bytes: int = 80_000_000,
) -> tuple[int, int]:
    """Rewrite JSONL keeping only executed-bet-related events. Returns (lines_kept, lines_dropped)."""
    if not log_path.is_file():
        return (0, 0)
    try:
        raw = log_path.read_bytes()
    except OSError:
        return (0, 0)
    if len(raw) > max_read_bytes:
        raw = raw[-max_read_bytes:]
    text = raw.decode("utf-8", errors="replace")
    lines_in = [ln.strip() for ln in text.splitlines() if ln.strip()]
    kept: list[str] = []
    for line in lines_in:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        ev = obj.get("event")
        if isinstance(ev, str) and ev in STRUCTURED_LOG_BET_EVENT_NAMES:
            kept.append(json.dumps(obj, ensure_ascii=False))

    dropped = len(lines_in) - len(kept)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = log_path.with_suffix(log_path.suffix + ".tmp")
    tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    tmp.replace(log_path)
    return (len(kept), max(0, dropped))


_summary_cache: tuple[tuple[float, int], dict[str, TickerOutcomeSummary]] | None = None


def load_ticker_outcome_summaries(
    path: Path,
    *,
    max_bytes: int = 12_000_000,
    max_lines: int = 120_000,
) -> dict[str, TickerOutcomeSummary]:
    """Aggregate win/loss/breakeven per ticker from ``auto_sell_profit_estimate`` lines (``pnl_outcome``)."""
    global _summary_cache
    if not path.exists():
        return {}
    try:
        st = path.stat()
        key = (st.st_mtime, st.st_size)
    except OSError:
        return {}
    if _summary_cache is not None and _summary_cache[0] == key:
        return _summary_cache[1]
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()[-max_lines:]

    out: dict[str, TickerOutcomeSummary] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("event") != "auto_sell_profit_estimate":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        t = payload.get("ticker")
        if not isinstance(t, str) or not t:
            continue
        po = payload.get("pnl_outcome")
        summ = out.setdefault(t, TickerOutcomeSummary())
        if po == "win":
            summ.wins += 1
        elif po == "loss":
            summ.losses += 1
        elif po == "breakeven":
            summ.breakevens += 1
        else:
            summ.unknown += 1
    _summary_cache = (key, out)
    return out


def bet_history_extra_min_edge(ticker: str, settings: Settings) -> float:
    """Add to required min net edge when ticker has recorded realized losses."""
    per = float(settings.trade_bet_history_edge_penalty_per_loss)
    cap = float(settings.trade_bet_history_max_edge_penalty)
    if per <= 0 or cap <= 0:
        return 0.0
    path = settings.structured_log_path
    summaries = load_ticker_outcome_summaries(
        path,
        max_bytes=int(settings.trade_bet_history_scan_max_bytes),
        max_lines=int(settings.trade_bet_history_scan_max_lines),
    )
    s = summaries.get(ticker)
    if s is None or s.losses <= 0:
        return 0.0
    return min(cap, per * float(s.losses))


def should_skip_ticker_for_bet_history(ticker: str, settings: Settings) -> bool:
    """Skip new entries when ticker has enough realized losses in the scanned log."""
    min_losses = int(settings.trade_bet_history_skip_ticker_min_losses)
    if min_losses <= 0:
        return False
    path = settings.structured_log_path
    summaries = load_ticker_outcome_summaries(
        path,
        max_bytes=int(settings.trade_bet_history_scan_max_bytes),
        max_lines=int(settings.trade_bet_history_scan_max_lines),
    )
    s = summaries.get(ticker)
    if s is None:
        return False
    return s.losses >= min_losses


def invalidate_outcome_summary_cache() -> None:
    """Call after rewriting structured log so the next read picks up new contents."""
    global _summary_cache
    _summary_cache = None
