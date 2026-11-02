"""Run the read-only downloader; see scripts/download.md."""

import sys

from fortuna_market_data.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["download", *sys.argv[1:]]))
