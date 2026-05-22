#!/usr/bin/env python3
"""CDR import CLI for cron and web (subprocess)."""
from __future__ import annotations

import argparse
import sys
from datetime import date

from import_runner import import_logger, reconcile_stale_running_batches, run_import_batch


def _parse_date(value: str | None) -> date | None:
    if not value or not value.strip():
        return None
    return date.fromisoformat(value.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SMSC CDR import batch")
    parser.add_argument("--from", dest="backfill_from", metavar="DATE", help="Backfill from YYYY-MM-DD")
    parser.add_argument("--to", dest="backfill_to", metavar="DATE", help="Backfill to YYYY-MM-DD")
    parser.add_argument(
        "--triggered-by",
        default="cron",
        choices=("cron", "web", "manual"),
        help="Who started this run",
    )
    args = parser.parse_args()

    reconcile_stale_running_batches()

    backfill_from = _parse_date(args.backfill_from)
    backfill_to = _parse_date(args.backfill_to)

    try:
        status = run_import_batch(
            backfill_from=backfill_from,
            backfill_to=backfill_to,
            triggered_by=args.triggered_by,
        )
    except RuntimeError as exc:
        import_logger.info("import skipped: %s", exc)
        print(f"skipped: {exc}")
        return 0

    print(f"import status: {status}")
    return 0 if status in ("success", "partial", "done", "skipped_running") else 1


if __name__ == "__main__":
    sys.exit(main())
