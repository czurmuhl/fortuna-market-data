# Validate a snapshot

`uv run python scripts/validate.py data/snapshots/provider/<run-id>`

Checks format, paths, file hashes, Parquet schemas, row counts and OHLCV validity.
It reads local files only. A successful check prints valid=true and the run ID.
This does not imply every security has complete history: inspect coverage.json;
Fortuna's adapter applies stricter session/strategy checks to a selected universe.
