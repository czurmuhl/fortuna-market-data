"""Verify a completed snapshot without HTTP; see scripts/validate.md."""

import sys

from fortuna_market_data.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["validate", *sys.argv[1:]]))
