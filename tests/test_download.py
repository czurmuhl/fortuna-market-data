from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from fortuna_market_data.client import Alpaca
from fortuna_market_data.download import download
from fortuna_market_data.fixture import respond
from fortuna_market_data.model import Config
from fortuna_market_data.storage import read_bars, verify


def config(**overrides: Any) -> Config:
    return Config.model_validate(
        {"start": "2024-01-01", "end": "2026-10-29", "symbols": ["SPY", "AAPL"], **overrides}
    )


def test_paginated_snapshot_and_incremental_update(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return respond(request)

    transport = httpx.MockTransport(capture)
    first = download(config(), tmp_path, fixture=True, transport=transport)
    manifest = verify(first)
    assert manifest["mode"] == "fixture" and manifest["feed"] == "iex"
    assert any("page_token" in request.url.params for request in calls)
    assert all(r.method == "GET" for r in calls)
    old = read_bars(first, manifest, "raw", ["SPY"])
    assert len(old) > 500
    calls.clear()
    second = download(
        config(end="2026-10-30"), tmp_path, fixture=True, previous=first, transport=transport
    )
    new_manifest = verify(second)
    new = read_bars(second, new_manifest, "raw", ["SPY"])
    assert len(new) == len(old) + 1
    assert len({row["day"] for row in new}) == len(new)
    assert all(
        r.url.params["start"].startswith("2026-10-22") for r in calls if "bars" in r.url.path
    )
    assert new_manifest["parent_manifest_sha256"]
    assert verify(first) == manifest  # previous immutable snapshot remains unchanged


def test_adjustment_change_refreshes_full_symbol_history(tmp_path: Path) -> None:
    first = download(config(), tmp_path, fixture=True)
    calls: list[httpx.Request] = []

    def split(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        response = respond(request)
        body = response.json()
        if request.url.path.endswith("/bars") and request.url.params["adjustment"] == "split":
            for records in body["bars"].values():
                for row in records:
                    for field in ("o", "h", "l", "c", "vw"):
                        row[field] /= 2
            return httpx.Response(200, json=body)
        return response

    second = download(
        config(end="2026-10-30"),
        tmp_path,
        fixture=True,
        previous=first,
        transport=httpx.MockTransport(split),
    )
    old = read_bars(first, verify(first), "split", ["SPY"])
    new = read_bars(second, verify(second), "split", ["SPY"])
    assert new[0]["close"] == old[0]["close"] / 2
    assert any(
        r.url.params["start"].startswith("2024-01-01") for r in calls if "bars" in r.url.path
    )


def test_resume_reuses_successful_pages_and_preserves_fetch_times(tmp_path: Path) -> None:
    attempts: list[str] = []
    fail = [True]

    def interrupted(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if request.url.path.endswith("/bars") and fail[0]:
            return httpx.Response(429, json={"message": "must not log this body"})
        return respond(request)

    transport = httpx.MockTransport(interrupted)
    with pytest.raises(ValueError, match="HTTP 429"):
        download(config(), tmp_path, fixture=True, transport=transport)
    assert not list((tmp_path / "snapshots").glob("*/*"))
    run = next((tmp_path / "work" / "fixture").iterdir())
    assert "must not log" not in (run / "events.jsonl").read_text()
    fail[0] = False
    result = download(config(), tmp_path, fixture=True, transport=transport, resume=run.name)
    assert attempts.count("/v2/assets") == 1 and attempts.count("/v2/calendar") == 1
    assert verify(result)["run_id"] == run.name
    with pytest.raises(ValueError, match="identical config"):
        download(config(end="2026-10-30"), tmp_path, fixture=True, resume=run.name)


def test_broad_discovery_is_bounded_and_records_metadata(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="symbol budget"):
        download(config(symbols=[], max_symbols=2), tmp_path, fixture=True)
    snapshot = download(config(symbols=[], start="2026-10-20"), tmp_path, fixture=True)
    manifest = verify(snapshot)
    assert len(manifest["symbols"]) == 11
    assets = json.loads((snapshot / "assets.json").read_text())
    assert all(a["class"] == "us_equity" for a in assets)
    assert all("is_etf" not in a for a in assets)  # never infer ETF exemptions


def test_missing_data_is_recorded_not_fabricated(tmp_path: Path) -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        response = respond(request)
        if request.url.path.endswith("/bars"):
            payload = response.json()
            payload["bars"].pop("AAPL", None)
            return httpx.Response(200, json=payload)
        return response

    snapshot = download(config(), tmp_path, fixture=True, transport=httpx.MockTransport(missing))
    coverage = json.loads((snapshot / "coverage.json").read_bytes())
    assert coverage["AAPL"]["raw"]["rows"] == 0
    assert coverage["AAPL"]["raw"]["missing_sessions"] > 0


def test_checksum_failure_and_path_traversal(tmp_path: Path) -> None:
    snapshot = download(config(start="2026-10-20"), tmp_path, fixture=True)
    path = snapshot / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"][0]["path"] = "../outside"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unsafe"):
        verify(snapshot)


def test_feed_mismatch_and_current_date_rejected(tmp_path: Path) -> None:
    snapshot = download(config(start="2026-10-20"), tmp_path, fixture=True)
    with pytest.raises(ValueError, match="incompatible"):
        download(config(start="2026-10-20", feed="sip"), tmp_path, fixture=True, previous=snapshot)
    with pytest.raises(ValueError, match="completed historical"):
        config(end=str(date.max))


def test_request_budget_repeated_pagination_and_invalid_prices(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="budget exhausted"):
        download(config(max_requests=1), tmp_path, fixture=True)

    def looping(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/bars"):
            return httpx.Response(200, json={"bars": {}, "next_page_token": "same"})
        return respond(request)

    with pytest.raises(ValueError, match="repeated pagination"):
        download(config(), tmp_path, fixture=True, transport=httpx.MockTransport(looping))

    def invalid(request: httpx.Request) -> httpx.Response:
        response = respond(request)
        if request.url.path.endswith("/bars"):
            body = response.json()
            next(iter(body["bars"].values()))[0]["c"] = -1
            return httpx.Response(200, json=body)
        return response

    with pytest.raises(ValueError, match="OHLC"):
        download(config(), tmp_path, fixture=True, transport=httpx.MockTransport(invalid))


def test_pacing_auth_and_endpoint_safety(tmp_path: Path) -> None:
    clock = [0.0]
    waits = []

    def pause(seconds: float) -> None:
        waits.append(seconds)
        clock[0] += seconds

    def check(request: httpx.Request) -> httpx.Response:
        assert request.headers["APCA-API-KEY-ID"] == "test-key"
        assert request.headers["APCA-API-SECRET-KEY"] == "test-secret"
        return httpx.Response(200, json=[])

    client = Alpaca(
        config(),
        tmp_path,
        "test-key",
        "test-secret",
        transport=httpx.MockTransport(check),
        sleep=pause,
        monotonic=lambda: clock[0],
    )
    try:
        client.get("paper", "/v2/assets", {})
        client.get("paper", "/v2/calendar", {})
        assert waits == [0.5]
        with pytest.raises(ValueError, match="unsupported endpoint"):
            client.get("paper", "/v2/orders", {})
        for cached in (tmp_path / "requests").glob("*.json"):
            assert "test-secret" not in cached.read_text()
            assert "test-key" not in cached.read_text()
    finally:
        client.close()
