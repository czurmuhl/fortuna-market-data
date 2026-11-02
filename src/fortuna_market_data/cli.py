"""Script entrypoints for data acquisition and offline verification."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from fortuna_market_data.download import download
from fortuna_market_data.logs import configure, event
from fortuna_market_data.model import Config
from fortuna_market_data.storage import verify


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("download")
    fetch.add_argument("--config", type=Path, required=True)
    fetch.add_argument("--output", type=Path, default=Path("data"))
    fetch.add_argument("--previous", type=Path)
    fetch.add_argument("--end", type=date.fromisoformat)
    fetch.add_argument("--fixture", action="store_true")
    fetch.add_argument("--resume", help="failed run ID; reuse successful HTTP pages")
    check = sub.add_parser("validate")
    check.add_argument("snapshot", type=Path)
    args = parser.parse_args(argv)
    configure()
    # Local .env is optional; existing process variables win. No credential values are logged.
    load_dotenv(Path.cwd() / ".env", override=False)
    awake = None
    try:
        if sys.platform == "darwin":
            awake = subprocess.Popen(["caffeinate", "-ims"])
        if args.command == "validate":
            result = verify(args.snapshot)
            print(
                json.dumps(
                    {"valid": True, "run_id": result["run_id"], "symbols": len(result["symbols"])}
                )
            )
        else:
            values = json.loads(args.config.read_bytes())
            if args.end:
                values["end"] = str(args.end)
            config = Config.model_validate(values)
            print(
                download(
                    config,
                    args.output,
                    previous=args.previous,
                    fixture=args.fixture,
                    resume=args.resume,
                )
            )
        return 0
    except Exception as exc:
        event("command.failed", error_type=type(exc).__name__)
        print(
            f"Data command failed ({type(exc).__name__}); inspect events.jsonl for run ID",
            file=sys.stderr,
        )
        return 1
    finally:
        if awake:
            awake.terminate()
            awake.wait()
