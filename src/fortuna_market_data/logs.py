"""Metadata-only JSON lineage. Never include credentials or provider error bodies."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_context: ContextVar[dict[str, Any] | None] = ContextVar("market_data_context", default=None)

logger = logging.getLogger("fortuna.market_data")


class Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "schema_version": "fortuna-lineage/v1",
                "component": "market_data",
                **(_context.get() or {}),
                "level": record.levelname.lower(),
                "event": record.getMessage(),
                **getattr(record, "fields", {}),
            },
            default=str,
        )


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def event(name: str, *, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, name, extra={"fields": fields})


def configure() -> None:
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(Formatter())
        logger.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
