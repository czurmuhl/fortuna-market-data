"""Bounded, paced, resumable Alpaca GETs; fixed hosts, no order methods."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from fortuna_market_data.logs import event
from fortuna_market_data.model import Config


class Alpaca:
    def __init__(
        self,
        config: Config,
        work: Path,
        key: str,
        secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not key or not secret:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        self.config, self.work = config, work
        self.client = httpx.Client(
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            timeout=30,
            follow_redirects=False,
            transport=transport,
        )
        self.sleep, self.now = sleep, monotonic
        self.last: float | None = None
        self.calls = 0
        self.fetched_at = datetime.now(UTC)
        (work / "requests").mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.client.close()

    def get(self, host: str, path: str, params: dict[str, str]) -> Any:
        if (host, path) not in {
            ("data", "/v2/stocks/bars"),
            ("paper", "/v2/assets"),
            ("paper", "/v2/calendar"),
        }:
            raise ValueError("unsupported endpoint")
        url = {"data": "https://data.alpaca.markets", "paper": "https://paper-api.alpaca.markets"}[
            host
        ] + path
        fingerprint = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
        cache = self.work / "requests" / (fingerprint + ".json")
        if cache.exists():
            envelope = json.loads(cache.read_bytes())
            body = envelope["body"].encode()
            if hashlib.sha256(body).hexdigest() != envelope["sha256"]:
                raise ValueError("request cache checksum mismatch")
            self.fetched_at = datetime.fromisoformat(envelope["fetched_at"])
            event("api.cache_hit", run_id=self.work.name, request_sha256=fingerprint)
            return json.loads(body)
        if self.calls >= self.config.max_requests:
            raise ValueError("request budget exhausted; committed snapshots remain unchanged")
        if self.last is not None:
            delay = 60 / self.config.requests_per_minute - (self.now() - self.last)
            if delay > 0:
                event("api.wait", run_id=self.work.name, seconds=round(delay, 3))
                self.sleep(delay)
        self.last = self.now()
        self.calls += 1
        request_id = uuid.uuid4().hex
        safe_params = {k: v for k, v in params.items() if k != "page_token"}
        event(
            "api.request",
            run_id=self.work.name,
            request_id=request_id,
            method="GET",
            endpoint=url,
            parameters=safe_params,
            pagination="page_token" in params,
        )
        started = self.now()
        with self.client.stream("GET", url, params=params) as response:
            event(
                "api.status",
                run_id=self.work.name,
                request_id=request_id,
                http_status=response.status_code,
            )
            if response.status_code != 200:
                raise ValueError(f"Alpaca HTTP {response.status_code}; no fallback or retry")
            body_array = bytearray()
            for chunk in response.iter_bytes():
                body_array.extend(chunk)
                if len(body_array) > 20_000_000:
                    raise ValueError("response exceeds 20 MB size limit")
            body = bytes(body_array)
        self.fetched_at = datetime.now(UTC)
        payload = json.loads(body)
        body_sha = hashlib.sha256(body).hexdigest()
        event(
            "api.response",
            run_id=self.work.name,
            request_id=request_id,
            bytes=len(body),
            sha256=body_sha,
            duration_ms=round((self.now() - started) * 1000, 3),
        )
        envelope = {
            "body": body.decode(),
            "sha256": body_sha,
            "fetched_at": self.fetched_at.isoformat(),
            "endpoint": url,
            "parameters": params,
        }
        temporary = cache.with_suffix(".tmp")
        temporary.write_text(json.dumps(envelope))
        temporary.replace(cache)
        return payload
