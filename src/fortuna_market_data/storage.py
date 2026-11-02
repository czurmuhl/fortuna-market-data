"""Immutable snapshot verification and selective Parquet reads."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

import pyarrow.parquet as pq

from fortuna_market_data.model import VERSION, Config, digest_file, schema, validate_bar


def contained(root: Path, name: str) -> Path:
    parts = PurePosixPath(name)
    if parts.is_absolute() or not parts.parts or any(p in (".", "..") for p in parts.parts):
        raise ValueError("unsafe snapshot path")
    root = root.resolve()
    path = root.joinpath(*parts.parts)
    if path.resolve() != path or not path.is_file():
        raise ValueError("snapshot entry must be a regular contained file")
    return path


def verify(root: Path) -> dict[str, Any]:
    manifest = json.loads((root / "manifest.json").read_bytes())
    if manifest.get("schema_version") != VERSION or manifest.get("provider") != "alpaca":
        raise ValueError("unsupported snapshot")
    if (
        manifest.get("mode") not in ("provider", "fixture")
        or manifest.get("feed") not in ("iex", "sip")
        or manifest.get("currency") != "USD"
        or manifest.get("timeframe") != "1Day"
    ):
        raise ValueError("unsupported snapshot metadata")
    symbols = manifest["symbols"]
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("invalid snapshot universe")
    entries = manifest["files"]
    names = [f["path"] for f in entries]
    if len(set(names)) != len(names):
        raise ValueError("duplicate manifest entries")
    if not {"assets.json", "calendar.json", "coverage.json", "config.json", "evidence.json"} <= set(
        names
    ):
        raise ValueError("missing metadata files")
    claimed: set[tuple[str, str]] = set()
    for entry in entries:
        path = contained(root, entry["path"])
        if digest_file(path) != entry["sha256"]:
            raise ValueError("snapshot checksum mismatch")
        if entry["kind"] == "bars":
            adjustment = entry["adjustment"]
            if adjustment not in ("raw", "split", "all"):
                raise ValueError("unsupported price adjustment")
            for symbol in entry["symbols"]:
                if symbol not in symbols or (adjustment, symbol) in claimed:
                    raise ValueError("duplicate or unexpected symbol partition")
                claimed.add((adjustment, symbol))
            table = pq.read_table(path)
            if not table.schema.equals(schema(entry["adjustment"]), check_metadata=True):
                raise ValueError("bar schema mismatch")
            if table.num_rows != entry["rows"]:
                raise ValueError("bar row count mismatch")
            keys: set[tuple[str, date]] = set()
            for row in table.to_pylist():
                key = row["ticker"], row["day"]
                if key in keys or row["ticker"] not in entry["symbols"]:
                    raise ValueError("duplicate or unexpected bar key")
                if (
                    not date.fromisoformat(manifest["start"])
                    <= row["day"]
                    <= date.fromisoformat(manifest["end"])
                ):
                    raise ValueError("bar outside snapshot dates")
                keys.add(key)
                validate_bar(row)
        elif entry["kind"] != "metadata":
            raise ValueError("unsupported file kind")
    if not {(a, s) for a in ("raw", "split") for s in symbols} <= claimed:
        raise ValueError("missing raw/split symbol partitions")
    config = Config.model_validate_json((root / "config.json").read_bytes())
    if (
        config.digest() != manifest["config_sha256"]
        or str(config.start) != manifest["start"]
        or str(config.end) != manifest["end"]
        or config.feed != manifest["feed"]
    ):
        raise ValueError("snapshot configuration mismatch")
    return dict(manifest)


def read_bars(
    root: Path, manifest: dict[str, Any], adjustment: str, symbols: list[str]
) -> list[dict[str, Any]]:
    rows = []
    for entry in manifest["files"]:
        if (
            entry["kind"] == "bars"
            and entry["adjustment"] == adjustment
            and set(symbols) & set(entry["symbols"])
        ):
            table = pq.read_table(
                contained(root, entry["path"]), filters=[("ticker", "in", symbols)]
            )
            rows.extend(table.to_pylist())
    return rows
