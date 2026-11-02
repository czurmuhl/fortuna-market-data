# Daily snapshot contract: fortuna-market-data/v1

A snapshot is an immutable local directory. `manifest.json` is its commit marker;
work/staging directories are not committed snapshots. Paths in the manifest are
relative, regular files inside the snapshot (no symlinks/traversal/duplicates).
All declared files must match their SHA256 hashes. Store snapshots by mode and
run ID; fixture snapshots cannot be mixed with provider history.

Manifest fields:

| Field | Meaning |
| --- | --- |
| schema_version | `fortuna-market-data/v1` |
| provider / mode | `alpaca`; `provider` or `fixture` |
| run_id / created_at | Attempt UUID and aware UTC commit timestamp |
| feed / currency / timeframe | Explicit `iex` or `sip`; `USD`; `1Day` |
| start / end | Inclusive historical session-date window; end is a completed session |
| symbols | Sorted unique snapshot universe, not implied historical membership |
| config_sha256 | Hash of normalized config JSON (sorted keys) |
| parent_manifest_sha256 | Previous snapshot manifest hash for updates, otherwise null |
| symbol_mapping | `asof=-`: no current-name alias mapping |
| adjustment_vintage | `retrieved_not_point_in_time` |
| files | Listed metadata/Parquet entries with path, kind and SHA256 |

Metadata: `assets.json` (provider asset records), `calendar.json` (provider calendar),
`coverage.json` (per-symbol/basis rows, first/last sessions and missing count),
`config.json` (non-secret config), `evidence.json` (request endpoint/parameters,
fetch time and body hash). Raw successful HTTP bodies are retained in the work
cache; evidence hashes connect these to acquisition. Parent manifests preserve
lineage of retained older bars. `asof` is symbol mapping, not a guarantee of
historical availability or corporate-action adjustment as of a past date.

Parquet entries have kind `bars`, adjustment `raw`, `split`, or optional `all`,
`symbols`, and total `rows`. Example path: `bars/split/00000.parquet`.
Chunks bound memory by batch size; a symbol occurs in one chunk per adjustment.
Schema metadata is exactly `format=fortuna-market-data/v1` and
`adjustment=<basis>`.

| Column | Arrow type | Meaning |
| --- | --- | --- |
| ticker | string | Provider symbol (not an inferred instrument category) |
| day | date32 | Session date in America/New_York, from the bar timestamp |
| open / high / low / close | float64 | Positive finite prices on the file's basis |
| volume | decimal128(24,6) | Nonnegative, with up to six fractional digits |
| vwap | nullable float64 | Positive finite provider VWAP, if supplied |
| transactions | nullable int64 | Nonnegative provider trade count, if supplied |
| loaded_at | timestamp(us), naive UTC | Fetch time of the response supplying the row |

Rows are sorted by ticker/day and unique within each basis. Missing bars remain
missing. Do not invent zero volume or carry forward prices to repair coverage.
`raw` is unadjusted, `split` adjusts for splits, `all` is Alpaca's separate
multi-action basis. Never silently exchange them. Overlap revisions to adjusted
bars cause a full symbol/basis refresh; older undetected revisions require a full
refresh. Each snapshot is a coherent retrieved dataset, not a verified point-in-
time corporate-action archive or complete delisting-return database.

Fortuna2 import checks hashes and schemas, uses the supplied calendar, requires
full selected-symbol session coverage, slices an explicit historical cutoff and
writes its existing bars/prices/manifest input contract. Source is `alpaca` for
provider data and `fixture` for mock data; feed and source snapshot hash remain
in the imported manifest and propagate into analytics. The selected ruleset is
separate from acquisition configuration. A cutoff limits bars, not the provider's
historically revised knowledge: use appropriate evidence before making causal
backtest claims.

Files are market data, not strategy parameters, account state or trade orders.
No credentials or account-position data belong in this format. Future datasets
(fundamentals/news/ratios) need availability timestamps and their own schemas.
