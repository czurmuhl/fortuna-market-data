"""Configuration and the provider-neutral daily Parquet contract."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, model_validator

VERSION = "fortuna-market-data/v1"
NY = ZoneInfo("America/New_York")
Symbol = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,15}$")]
Adjustment = Literal["raw", "split", "all"]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start: date
    end: date
    feed: Literal["iex", "sip"] = "iex"
    symbols: list[Symbol] = Field(default_factory=list)
    exchanges: list[str] = Field(default_factory=lambda: ["NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"])
    adjustments: list[Adjustment] = Field(
        default_factory=lambda: list[Adjustment](["raw", "split"])
    )
    requests_per_minute: int = Field(default=120, ge=1, le=10000)
    max_requests: int = Field(default=2000, ge=1, le=100000)
    max_symbols: int = Field(default=10000, ge=1, le=20000)
    batch_size: int = Field(default=50, ge=1, le=200)
    overlap_days: int = Field(default=7, ge=1, le=365)

    @model_validator(mode="after")
    def valid(self) -> Config:
        if self.start > self.end or self.end >= datetime.now(NY).date():
            raise ValueError("use an explicit completed historical date range")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("duplicate symbols")
        if not {"raw", "split"} <= set(self.adjustments):
            raise ValueError("raw and split prices are required")
        if len(set(self.adjustments)) != len(self.adjustments):
            raise ValueError("duplicate adjustments")
        if len(self.symbols) > self.max_symbols:
            raise ValueError("symbol budget exceeded")
        return self

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()


def schema(adjustment: str) -> Any:
    return pa.schema(
        [
            ("ticker", pa.string()),
            ("day", pa.date32()),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("volume", pa.decimal128(24, 6)),
            ("vwap", pa.float64()),
            ("transactions", pa.int64()),
            ("loaded_at", pa.timestamp("us")),
        ],
        metadata={b"format": VERSION.encode(), b"adjustment": adjustment.encode()},
    )


def digest_file(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def validate_bar(row: dict[str, Any]) -> None:
    import math

    values = [row[k] for k in ("open", "high", "low", "close")]
    if any(v is None or not math.isfinite(float(v)) or v <= 0 for v in values):
        raise ValueError("invalid OHLC price")
    if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]):
        raise ValueError("invalid OHLC range")
    volume = Decimal(str(row["volume"]))
    if not volume.is_finite() or volume < 0 or volume != volume.quantize(Decimal("0.000001")):
        raise ValueError("invalid volume precision")
    if row["vwap"] is not None and (not math.isfinite(row["vwap"]) or row["vwap"] <= 0):
        raise ValueError("invalid VWAP")
    if row["transactions"] is not None and (
        type(row["transactions"]) is not int or row["transactions"] < 0
    ):
        raise ValueError("invalid transaction count")
