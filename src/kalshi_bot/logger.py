"""Structured JSON logging for decisions and execution events."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


def _json_default(obj: object) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def _sanitize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {k: _sanitize(v) for k, v in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize(v) for v in value]
    return str(value)


@dataclass
class JsonLogRecord:
    """One line in the JSONL log file."""

    ts: str
    level: str
    event: str
    payload: dict[str, Any]


class StructuredLogger:
    """Emits JSON lines to a file and human-readable lines to stderr."""

    def __init__(self, name: str, *, log_path: Path, level: str = "INFO") -> None:
        self._log = logging.getLogger(name)
        self._log.setLevel(getattr(logging, level.upper(), logging.INFO))
        self._log.handlers.clear()

        self._log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)

        fmt = logging.Formatter("%(message)s")
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(self._log.level)
        sh.setFormatter(fmt)
        self._log.addHandler(sh)

    def log_event(self, event: str, level: str = "INFO", **payload: Any) -> None:
        """Write a structured JSON record."""
        record = JsonLogRecord(
            ts=datetime.now(UTC).isoformat(),
            level=level.upper(),
            event=event,
            payload=_sanitize(payload),
        )
        line = json.dumps(asdict(record), default=_json_default)
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._log.log(getattr(logging, level.upper(), logging.INFO), f"{event} {payload}")

    def info(self, event: str, **payload: Any) -> None:
        self.log_event(event, "INFO", **payload)

    def warning(self, event: str, **payload: Any) -> None:
        self.log_event(event, "WARNING", **payload)

    def error(self, event: str, **payload: Any) -> None:
        self.log_event(event, "ERROR", **payload)


def get_logger(name: str, *, log_path: Path, level: str) -> StructuredLogger:
    return StructuredLogger(name, log_path=log_path, level=level)


def maybe_clear_structured_log_after_tickers(
    *,
    log_path: Path,
    every_n: int,
    processed_count: int,
    log: StructuredLogger | None = None,
) -> None:
    """Truncate the JSONL file every ``every_n`` processed tickers (multi-ticker scans). ``every_n`` 0 = disabled."""
    if every_n <= 0 or processed_count <= 0 or processed_count % every_n != 0:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    if log is not None:
        log.info(
            "structured_log_cleared",
            reason="every_n_tickers",
            every_n_tickers=every_n,
            processed_tickers=processed_count,
        )
