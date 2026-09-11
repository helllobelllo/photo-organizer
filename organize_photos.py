"""Command-line interface for the photo organizer.

Defaults to a dry run. Pass --run to actually touch files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import core


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Organize photos into Country folders using EXIF metadata."
    )
    parser.add_argument("source", type=Path, help="Folder to scan for photos (searched recursively).")
    parser.add_argument("destination", type=Path, help="Folder to organize photos into.")
    parser.add_argument(
        "--travel-log", type=Path, default=None,
        help="Excel travel log used for photos that have no GPS data.",
    )
    parser.add_argument(
        "--copy", action="store_true",
        help="Copy instead of moving (the input folder keeps its photos).",
    )
    parser.add_argument(
        "--group-by-year", action="store_true",
        help="Add a year subfolder inside each country folder.",
    )
    parser.add_argument(
        "--run", action="store_true",
        help="Actually move/copy files. Without this flag the script only previews.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if not args.source.is_dir():
        print(f"Source folder does not exist: {args.source}", file=sys.stderr)
        return 1

    plans, summary = core.build_plan(
        args.source, args.destination, args.travel_log, args.group_by_year
    )

    if not plans:
        print("No photos found.")
        return 0

    action = ("COPY" if args.copy else "MOVE") if args.run else "DRY RUN"
    print(f"\n{action} - {len(plans)} photo(s)\n")

    for plan in plans:
        relative = plan.destination_path.relative_to(args.destination)
        marker = "!" if plan.needs_review else " "
        print(f" {marker} {plan.source_path.name}  ->  {relative}")
        if plan.needs_review:
            print(f"     reason: {plan.review_reason}")

    for warning in summary.warnings:
        print(f"\nWarning: {warning}")

    if args.run:
        core.execute_plan(plans, summary, move=not args.copy)
        if not args.copy:
            core.remove_empty_subfolders(args.source)
        log_path = core.write_review_log(args.destination, plans, summary)
        print(f"\nLog written to: {log_path}")
    else:
        print("\nThis was a preview. Re-run with --run to move the files.")

    print(
        f"\nTotal: {summary.total} | GPS: {summary.by_gps} | "
        f"Travel log: {summary.by_travel_log} | Needs review: {summary.needs_review}"
    )
    if summary.failed:
        print(f"Failed: {len(summary.failed)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
