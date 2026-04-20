"""CLI entry point invoked by the systemd service."""

from __future__ import annotations

import logging
import sys

from .config import Config, ConfigError
from .sync import run_sync


def main() -> int:
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
        result = run_sync(config)
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
