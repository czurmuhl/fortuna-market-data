# Download daily data

Prerequisites: Python 3.13, uv and `uv sync --locked`. Set ALPACA_API_KEY and
ALPACA_SECRET_KEY in the process or local `.env`; fixture mode needs no keys.

`uv run python scripts/download.py --config config/example.json --output data`

Use `config/broad.json` for discovery of currently active tradable listed assets.
`--end YYYY-MM-DD` chooses a completed trading session; today is excluded.
`--previous PATH` updates from a verified snapshot of the same mode/feed/start.
`--resume RUN_ID` reuses successful request pages after an interrupted run; pass
identical config/previous arguments. Request budgets are per invocation.
`--fixture` uses synthetic bars and calendar sessions through a mock transport.

The last output line is the immutable snapshot directory. Metadata JSON logging
is written to stderr and `data/work/<mode>/<run-id>/events.jsonl`. Incomplete
runs have no published manifest. Keep the work directory for resuming and raw
response provenance. No broker orders, Massive calls or silent feed fallbacks.
