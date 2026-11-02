"""Download batches, resume requests, and commit a portable immutable snapshot."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import TypeAdapter

from fortuna_market_data.client import Alpaca
from fortuna_market_data.logs import Formatter, bind, event, logger
from fortuna_market_data.model import NY, VERSION, Config, Symbol, digest_file, schema, validate_bar
from fortuna_market_data.storage import read_bars, verify


def bars(
    client: Alpaca, config: Config, symbols: list[str], adjustment: str, start: date
) -> list[dict[str, Any]]:
    params = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "feed": config.feed,
        "adjustment": adjustment,
        "asof": "-",
        "sort": "asc",
        "limit": "10000",
        "start": datetime.combine(start, time.min, NY).isoformat(),
        "end": datetime.combine(config.end, time.max, NY).isoformat(),
    }
    rows: dict[tuple[str, date], dict[str, Any]] = {}
    tokens: set[str] = set()
    while True:
        payload = client.get("data", "/v2/stocks/bars", params)
        values = payload.get("bars")
        if not isinstance(values, dict) or not set(values) <= set(symbols):
            raise ValueError("unexpected symbols or invalid bar response")
        for ticker, records in values.items():
            for bar in records:
                instant = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    raise ValueError("bar timestamp must include timezone")
                day = instant.astimezone(NY).date()
                if not start <= day <= config.end:
                    raise ValueError("bar outside request window")
                row = {
                    "ticker": ticker,
                    "day": day,
                    **{
                        name: float(bar[key])
                        for name, key in [
                            ("open", "o"),
                            ("high", "h"),
                            ("low", "l"),
                            ("close", "c"),
                        ]
                    },
                    "volume": Decimal(str(bar["v"])),
                    "vwap": float(bar["vw"]) if bar.get("vw") is not None else None,
                    "transactions": bar.get("n"),
                    "loaded_at": client.fetched_at.astimezone(UTC).replace(tzinfo=None),
                }
                validate_bar(row)
                key = ticker, day
                if key in rows:
                    raise ValueError("duplicate ticker/session across pages")
                rows[key] = row
        token = payload.get("next_page_token")
        event(
            "bars.page",
            run_id=client.work.name,
            adjustment=adjustment,
            symbols=len(values),
            rows=sum(len(v) for v in values.values()),
            more=bool(token),
        )
        if not token:
            break
        if not isinstance(token, str) or token in tokens:
            raise ValueError("invalid or repeated pagination token")
        tokens.add(token)
        params["page_token"] = token
    return list(rows.values())


def discover(client: Alpaca, config: Config) -> tuple[list[str], list[dict[str, Any]]]:
    assets = client.get("paper", "/v2/assets", {"status": "active", "asset_class": "us_equity"})
    if not isinstance(assets, list):
        raise ValueError("invalid asset response")
    # us_equity includes ETFs, ADRs, preferred shares etc; never invent an exemption.
    available = {a["symbol"]: a for a in assets if a.get("class") == "us_equity"}
    if config.symbols:
        if not set(config.symbols) <= set(available):
            raise ValueError("explicit symbol missing from asset response")
        symbols = config.symbols
    else:
        symbols = []
        for symbol, asset in available.items():
            if asset.get("tradable") and asset.get("exchange") in config.exchanges:
                try:
                    TypeAdapter(Symbol).validate_python(symbol)
                except ValueError:
                    event("universe.unsupported_symbol", run_id=client.work.name, symbol=symbol)
                    continue
                symbols.append(symbol)
    symbols = sorted(set(symbols))
    if not symbols or len(symbols) > config.max_symbols:
        raise ValueError("empty universe or symbol budget exceeded; no silent truncation")
    event(
        "universe.selected",
        run_id=client.work.name,
        symbols=len(symbols),
        description="current active asset universe, not point-in-time membership",
    )
    return symbols, [available[s] for s in symbols]


def _execute(
    client: Alpaca, config: Config, staging: Path, previous: Path | None
) -> dict[str, Any]:
    prior = verify(previous) if previous else None
    if prior and (
        prior["feed"] != config.feed
        or prior["mode"] != client.work.parent.name
        or prior["start"] != str(config.start)
        or date.fromisoformat(prior["end"]) > config.end
    ):
        raise ValueError("previous snapshot feed/mode/start/date is incompatible")
    symbols, assets = discover(client, config)
    calendar = client.get(
        "paper", "/v2/calendar", {"start": str(config.start), "end": str(config.end)}
    )
    days = [date.fromisoformat(v["date"]) for v in calendar]
    if (
        len(set(days)) != len(days)
        or config.end not in days
        or any(not config.start <= d <= config.end for d in days)
    ):
        raise ValueError("invalid calendar or end is not an exchange session")
    sessions = set(days)
    coverage: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    for index in range(0, len(symbols), config.batch_size):
        batch = symbols[index : index + config.batch_size]
        for adjustment in config.adjustments:
            old = read_bars(previous, prior, adjustment, batch) if previous and prior else []
            old_keys = {(r["ticker"], r["day"]): r for r in old}
            existing = {r["ticker"] for r in old}
            newcomers = set(batch) - existing
            start = (
                max(
                    config.start,
                    date.fromisoformat(prior["end"]) - timedelta(days=config.overlap_days),
                )
                if prior
                else config.start
            )
            fresh = bars(client, config, batch, adjustment, start)
            if prior and start > config.start and newcomers:
                fresh += bars(client, config, sorted(newcomers), adjustment, config.start)
            if adjustment != "raw" and old:
                changed = {
                    r["ticker"]
                    for r in fresh
                    if (r["ticker"], r["day"]) in old_keys
                    and any(
                        r[c] != old_keys[r["ticker"], r["day"]][c]
                        for c in ("open", "high", "low", "close", "volume", "vwap")
                    )
                }
                if changed:
                    event(
                        "adjustment.refresh",
                        run_id=client.work.name,
                        adjustment=adjustment,
                        symbols=sorted(changed),
                    )
                    old_keys = {k: v for k, v in old_keys.items() if k[0] not in changed}
                    fresh = [r for r in fresh if r["ticker"] not in changed] + bars(
                        client, config, sorted(changed), adjustment, config.start
                    )
            # Remove the refreshed window first, so disappeared bars do not remain cached.
            merged = {k: v for k, v in old_keys.items() if k[1] < start}
            for row in fresh:
                if row["day"] not in sessions:
                    raise ValueError("bar on a non-session date")
                merged[row["ticker"], row["day"]] = row
            data = sorted(merged.values(), key=lambda r: (r["ticker"], r["day"]))
            relative = f"bars/{adjustment}/{index:05d}.parquet"
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pylist(data, schema=schema(adjustment)), path)
            files.append(
                {
                    "path": relative,
                    "kind": "bars",
                    "adjustment": adjustment,
                    "symbols": batch,
                    "rows": len(data),
                    "sha256": digest_file(path),
                }
            )
            for symbol in batch:
                observed = {r["day"] for r in data if r["ticker"] == symbol}
                coverage.setdefault(symbol, {})[adjustment] = {
                    "rows": len(observed),
                    "first": str(min(observed)) if observed else None,
                    "last": str(max(observed)) if observed else None,
                    "missing_sessions": len(sessions - observed),
                }
            event(
                "batch.saved",
                run_id=client.work.name,
                adjustment=adjustment,
                symbols=len(batch),
                rows=len(data),
                path=relative,
            )
    evidence = []
    for cache in sorted((client.work / "requests").glob("*.json")):
        record = json.loads(cache.read_bytes())
        evidence.append({k: v for k, v in record.items() if k != "body"})
    for name, value in {
        "assets.json": assets,
        "calendar.json": calendar,
        "coverage.json": coverage,
        "config.json": config.model_dump(mode="json"),
        "evidence.json": evidence,
    }.items():
        path = staging / name
        path.write_text(json.dumps(value, indent=2) + "\n")
        files.append({"path": name, "kind": "metadata", "sha256": digest_file(path)})
    return {
        "schema_version": VERSION,
        "provider": "alpaca",
        "mode": client.work.parent.name,
        "run_id": client.work.name,
        "feed": config.feed,
        "currency": "USD",
        "timeframe": "1Day",
        "start": str(config.start),
        "end": str(config.end),
        "symbol_mapping": "asof=-",
        "adjustment_vintage": "retrieved_not_point_in_time",
        "created_at": datetime.now(UTC).isoformat(),
        "symbols": symbols,
        "config_sha256": config.digest(),
        "files": files,
        "parent_manifest_sha256": digest_file(previous / "manifest.json") if previous else None,
    }


def download(
    config: Config,
    output: Path,
    *,
    previous: Path | None = None,
    fixture: bool = False,
    resume: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    with bind(mode="fixture" if fixture else "provider"):
        return _download(
            config, output, previous=previous, fixture=fixture, resume=resume, transport=transport
        )


def _download(
    config: Config,
    output: Path,
    *,
    previous: Path | None = None,
    fixture: bool = False,
    resume: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    # One writer for this data root, across fixture and provider invocations.
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_id = resume or uuid.uuid4().hex
        if len(run_id) > 80 or not run_id or any(c not in "0123456789abcdef-" for c in run_id):
            raise ValueError("invalid resume identifier")
        mode = "fixture" if fixture else "provider"
        work = output / "work" / mode / run_id
        work.mkdir(parents=True, exist_ok=True)
        identity = {
            "config_sha256": config.digest(),
            "parent": digest_file(previous / "manifest.json") if previous else None,
        }
        state = work / "identity.json"
        if resume and (not state.exists() or json.loads(state.read_bytes()) != identity):
            raise ValueError("resume must use identical config and previous snapshot")
        state.write_text(json.dumps(identity))
        destination = output / "snapshots" / mode / run_id
        if destination.exists():
            verify(destination)
            return destination
        staging = work / "staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        handler = logging.FileHandler(work / "events.jsonl")
        handler.setFormatter(Formatter())
        logger.addHandler(handler)
        client: Alpaca | None = None
        try:
            event("job.started", run_id=run_id, mode=mode, config_sha256=config.digest())
            options: dict[str, Any] = {}
            if fixture:
                from fortuna_market_data.fixture import respond

                clock = [0.0]

                def advance(seconds: float) -> None:
                    event("fixture.clock_advanced", run_id=run_id, seconds=seconds)
                    clock[0] += seconds

                options = {
                    "transport": transport or httpx.MockTransport(respond),
                    "sleep": advance,
                    "monotonic": lambda: clock[0],
                }
            elif transport is not None:
                options["transport"] = transport
            client = Alpaca(
                config,
                work,
                "fixture" if fixture else os.environ.get("ALPACA_API_KEY", ""),
                "fixture" if fixture else os.environ.get("ALPACA_SECRET_KEY", ""),
                **options,
            )
            manifest = _execute(client, config, staging, previous)
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            verify(staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(destination)
            event(
                "job.completed",
                run_id=run_id,
                mode=mode,
                snapshot=str(destination),
                calls=client.calls,
            )
            return destination
        except BaseException as exc:
            event(
                "job.failed",
                level=logging.ERROR,
                run_id=run_id,
                mode=mode,
                error_type=type(exc).__name__,
                resume=run_id,
            )
            raise
        finally:
            if client:
                client.close()
            logger.removeHandler(handler)
            handler.close()
