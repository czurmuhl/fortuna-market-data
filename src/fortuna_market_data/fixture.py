"""Synthetic Alpaca transport with real session dates and pagination; never sockets."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from typing import Any

import exchange_calendars as calendars
import httpx

from fortuna_market_data.model import NY

SYMBOLS = ["AAPL", "EEM", "EFA", "GLD", "IEF", "IWM", "MSFT", "QQQ", "SPY", "TLT", "VNQ"]


def respond(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    if request.url.path == "/v2/assets":
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": s,
                    "class": "us_equity",
                    "tradable": True,
                    "status": "active",
                    "exchange": "NYSE",
                    "name": "Synthetic " + s,
                }
                for s in SYMBOLS
            ],
        )
    if request.url.path == "/v2/calendar":
        start, end = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
    elif request.url.path == "/v2/stocks/bars":
        start, end = (datetime.fromisoformat(params[k]).date() for k in ("start", "end"))
    else:
        raise ValueError("unsupported fixture route")
    calendar = calendars.get_calendar(
        "XNYS", start=str(start - timedelta(days=7)), end=str(end + timedelta(days=7))
    )
    days = [v.date() for v in calendar.sessions_in_range(str(start), str(end))]
    if request.url.path == "/v2/calendar":
        return httpx.Response(
            200, json=[{"date": str(d), "open": "09:30", "close": "16:00"} for d in days]
        )
    rows: list[tuple[str, dict[str, Any]]] = []
    for symbol in sorted(params["symbols"].split(",")):
        seed = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
        for day in days:
            close = round(
                50 + seed % 50 + (day - date(2016, 1, 1)).days * (0.01 + seed % 7 * 0.001), 4
            )
            rows.append(
                (
                    symbol,
                    {
                        "t": datetime.combine(day, time.min, NY).isoformat(),
                        "o": close,
                        "h": close + 1,
                        "l": close - 1,
                        "c": close,
                        "v": 1000000,
                        "vw": close,
                        "n": 1000,
                    },
                )
            )
    offset = int(params.get("page_token", "0"))
    limit = min(int(params["limit"]), 500)  # exercise multiple pages even on small runs
    bars: dict[str, list[Any]] = {}
    for symbol, row in rows[offset : offset + limit]:
        bars.setdefault(symbol, []).append(row)
    token = str(offset + limit) if offset + limit < len(rows) else None
    return httpx.Response(200, json={"bars": bars, "next_page_token": token})
