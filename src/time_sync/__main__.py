"""CLI entry point invoked by the systemd service."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from .config import Config, ConfigError
from .state import _parse_iso
from .sync import run_sync


def _parse_since(value: str) -> datetime:
    parsed = _parse_iso(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(
            f"--since must be ISO 8601 (e.g. 2026-04-20 or 2026-04-20T00:00:00Z): {value!r}"
        )
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(prog="time-sync", description=__doc__)
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        metavar="DATE",
        help=(
            "Force this run to scan Clockify entries from DATE onward, ignoring the "
            "saved watermark. Accepts YYYY-MM-DD or full ISO 8601. State (synced IDs + "
            "Toggl [clk:<id>] scan) still dedupes, so re-runs are safe."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_env()
    except ConfigError as exc:
        logging.error("configuration error: %s", exc)
        return 2

    try:
        result = run_sync(config, force_since=args.since)
    except Exception:  # noqa: BLE001
        logging.exception("sync failed")
        return 1

    logging.info(
        "sync complete: fetched=%d created=%d skipped=%d failed=%d",
        result.fetched, result.created, result.skipped, result.failed,
    )
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
