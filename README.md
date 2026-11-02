# Fortuna market data

Script-driven, read-only Alpaca daily-data downloads for Fortuna research.
Download once, reuse the same verified snapshot for many strategy experiments.
No database, daemon, trading engine or order submission is required here.

## Setup

Use Python 3.13 and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked
cp .env.example .env
chmod 600 .env
```

Set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in `.env` using your Alpaca keys.
The scripts load this local file without overriding existing process variables.
Credentials, downloaded data and response caches are git-ignored.

Start without keys or network calls:

```bash
uv run python scripts/download.py --config config/example.json --output data --fixture
uv run python scripts/validate.py data/snapshots/fixture/<run-id>
```

Then download actual history:

```bash
uv run python scripts/download.py --config config/example.json --output data
```

The last line prints the committed snapshot directory. Progress JSON appears on
stderr and is retained in `data/work/<mode>/<run-id>/events.jsonl`.
`--fixture` uses synthetic Alpaca responses, real XNYS session dates, pagination
and a virtual pacing clock. It verifies engineering behavior, not strategy alpha.

## Broad data and daily refresh

`config/example.json` starts with seven equities/ETFs. `config/broad.json` selects
currently active tradable US-equity-class instruments from configured exchanges.
It includes ETFs and other securities; Alpaca's `us_equity` classification does
not identify ETF exemptions. Asset metadata and membership are preserved as
retrieved. This is not a historical, survivorship-free investable universe.

```bash
uv run python scripts/download.py --config config/broad.json --output data \
  --end YYYY-MM-DD
```

Replace `YYYY-MM-DD` with a completed, published trading session. Today and
non-session end dates are rejected. Discovering more than `max_symbols` fails
rather than silently truncating. `max_requests`, `batch_size` and
`requests_per_minute` bound costs and memory. Defaults are conservative settings,
not a statement of account entitlements; first validate the small example.

For the next session, pass the previous immutable snapshot:

```bash
uv run python scripts/download.py --config config/example.json --output data \
  --end YYYY-MM-DD --previous data/snapshots/provider/<previous-run-id>
```

Raw and adjusted bars refresh an overlap window. New symbols bootstrap from the
configured start. If overlapping adjusted bars change, the affected symbols'
full adjusted history is reloaded, avoiding mixed adjustment bases. Corrections
outside the overlap are not guaranteed to be detected: periodically omit
`--previous` to perform a complete refresh. No in-place snapshot edits occur.
The same start/feed/mode must be used for incremental updates.

On failure, no new snapshot is published. Resume with the logged run ID and
identical arguments/config:

```bash
uv run python scripts/download.py --config config/example.json --output data \
  --resume <failed-run-id>
```

Successful HTTP pages are checksum-verified and reused. HTTP 401/403/429 and 5xx
stop without automatic retries or feed fallback. Request budgets/rate spacing
apply to this invocation; coordinate other processes/accounts independently.
One writer holds a local lock on the data root. Work caches retain raw successful
responses for lineage and resuming; they are excluded from Git.

## Feed and price basis

`iex` is the explicit default; choose `sip` only if your account permits it.
Single-exchange IEX volume is not consolidated US volume. Authentication failure
or entitlement errors never change the feed behind your back.

Every snapshot contains `raw` and `split` daily OHLCV, optional VWAP/trade counts,
asset metadata, trading calendar, coverage, normalized config, request evidence,
and a manifest with hashes. Optional `all` prices are separately labeled and
never substituted for raw reference prices or split-adjusted analytics.
Historical adjustments are retrieved versions, not point-in-time knowledge.
`asof=-` disables symbol alias mapping; renamed symbols/history may need explicit
curation. See [the portable format](docs/FORMAT.md).

Missing history, IPO gaps and IEX sparse sessions are recorded in `coverage.json`,
not filled with invented bars. A broad snapshot can be valid with incomplete
securities; the Fortuna adapter rejects incomplete selected symbols.

## Fortuna2 handoff

With the corresponding Fortuna2 adapter installed, no API access is needed:

```bash
# From the Fortuna2 checkout:
uv run fortuna import-market-data \
  --snapshot /absolute/path/to/fortuna-market-data/data/snapshots/provider/<run-id> \
  --ruleset examples/local-ruleset.json --output-dir /tmp/fortuna-input
uv run fortuna local-analyze --input-dir /tmp/fortuna-input \
  --ruleset examples/local-ruleset.json --output-dir /tmp/fortuna-analytics
```

Use `--as-of YYYY-MM-DD` and optional `--start YYYY-MM-DD` on import to select a
historical cutoff without redownloading. The adapter selects only the ruleset's
candidates and benchmark, carries source/feed/snapshot hashes, and preserves
raw reference closes separately from split-adjusted bars. It retains Fortuna's
bounded local-analysis limits (100 candidates; 150,000 input rows); broad universe
collection does not mean broad automatic strategy search is implemented.

Container use: bind-mount the snapshot read-only into the existing Fortuna image
and use a writable exchange/output directory. No new ingestion container is needed.
A downloaded snapshot directory can also be transported via S3; publish its
manifest last after all immutable files, and download/verify it before import.
An automated S3 publisher and scheduler are not included in this bootstrap.

## Architecture boundaries

This repository owns acquisition and reproducible snapshots. Fortuna2 owns
numeric rules/analytics. The portfolio manager will own sizing, reconciled lots,
and paper/live execution. No broker orders are made by these scripts; only
`GET /v2/assets`, `GET /v2/calendar`, and `GET /v2/stocks/bars` are allowed.
Fundamentals, ratios and news are future independently versioned datasets, not
columns guessed from price bars. Historical replay/backtest execution is also
separate future work. Respect FIFO sells, 31-day equity holds, no ETF minimum
hold, and no shorts in the eventual portfolio/execution layer.

Alpaca documentation: [bars](https://docs.alpaca.markets/us/reference/stockbars),
[assets](https://docs.alpaca.markets/us/reference/get-v2-assets-1),
[plans/feed access](https://docs.alpaca.markets/us/docs/about-market-data-api).
Access depends on your account; source data is not republished in this repository.

## Checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

Tests use mock transport only; no real provider credentials or live orders.
