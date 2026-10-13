"""Command line for the T-100 pipeline.

Examples:
    # Download one month to disk, to check size and contents
    python -m t100_agent.ingest.run scrape --dataset segment --from 2024-01 --to 2024-01

    # Download, load into BigQuery and delete the local file, month by month
    python -m t100_agent.ingest.run sync --from 2015-01 --to 2025-06 --delete-local

    # Monthly job: continue from the latest month already in BigQuery
    python -m t100_agent.ingest.run sync --delete-local
"""

import argparse
from datetime import date

from .config import Settings, month_path
from .schema import DATASETS


def _parse_month(text: str) -> tuple[int, int]:
    y, m = text.split("-")
    return int(y), int(m)


def _months(start: tuple[int, int], end: tuple[int, int]):
    y, m = start
    while (y, m) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _datasets(arg: str) -> tuple[str, ...]:
    return DATASETS if arg == "both" else (arg,)


def cmd_scrape(args, settings: Settings) -> None:
    from .scrape import scrape_month

    for dataset in _datasets(args.dataset):
        for y, m in _months(_parse_month(args.start), _parse_month(args.end)):
            scrape_month(dataset, y, m, settings, force=args.force)


def cmd_load(args, settings: Settings) -> None:
    from .bigquery_load import ensure_dataset, get_client, load_month

    client = get_client(settings)
    ensure_dataset(client, settings)
    for dataset in _datasets(args.dataset):
        for y, m in _months(_parse_month(args.start), _parse_month(args.end)):
            path = month_path(settings.data_dir, dataset, y, m)
            if not path.exists():
                print(f"Missing on disk, skipping: {path}")
                continue
            load_month(client, settings, dataset, y, m, path, replace=args.replace)


def cmd_sync(args, settings: Settings) -> None:
    from .bigquery_load import ensure_dataset, get_client, load_month, loaded_months
    from .scrape import scrape_month

    client = get_client(settings)
    ensure_dataset(client, settings)
    today = date.today()
    end = _parse_month(args.end) if args.end else (today.year, today.month)

    for dataset in _datasets(args.dataset):
        done = loaded_months(client, settings, dataset)
        if args.start:
            start = _parse_month(args.start)
        elif done:
            y, m = max(done)
            start = (y + 1, 1) if m == 12 else (y, m + 1)
        else:
            raise SystemExit(f"No {dataset} data in BigQuery yet; pass --from YYYY-MM")

        for y, m in _months(start, end):
            if (y, m) in done:
                continue
            try:
                path = scrape_month(dataset, y, m, settings)
            except Exception as e:
                # BTS publishes with a lag of a few months; the first missing month ends the run.
                print(f"Stopping {dataset} at {y}-{m:02d}: {e}")
                break
            load_month(client, settings, dataset, y, m, path)
            if args.delete_local:
                path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="T-100 ingestion pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("scrape", "load", "sync"):
        p = sub.add_parser(name)
        p.add_argument("--dataset", choices=["market", "segment", "both"], default="both")
        p.add_argument("--from", dest="start", required=name != "sync", help="YYYY-MM")
        p.add_argument("--to", dest="end", required=name != "sync", help="YYYY-MM")
        if name == "scrape":
            p.add_argument("--force", action="store_true", help="re-download months on disk")
        if name == "load":
            p.add_argument("--replace", action="store_true", help="reload months already loaded")
        if name == "sync":
            p.add_argument(
                "--delete-local",
                action="store_true",
                help="delete each Parquet file after it is loaded, to save disk space",
            )

    args = parser.parse_args()
    settings = Settings.from_env()
    {"scrape": cmd_scrape, "load": cmd_load, "sync": cmd_sync}[args.command](args, settings)


if __name__ == "__main__":
    main()
