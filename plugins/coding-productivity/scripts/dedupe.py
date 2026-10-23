#!/usr/bin/env python3
"""
Detect duplicate work in the commits table and record it.

Applies the five duplicate-work rules (merge, squash, promotion, copy, moved
code; see ``lib/duplicate_work.py`` and the README) to everything in storage
and rebuilds three tables wholesale: ``duplicate_commits``,
``file_moved_share`` and the ``dedupe_runs`` completion marker.
``analyze.py`` then excludes the duplicate commits and discounts moved code.

Run it after extraction and scoring, and again whenever new commits arrive:
the rules compare each commit with all the others, so a partial run is not
meaningful.

Usage::

    python dedupe.py --config .coding-productivity.env
    python dedupe.py --config .coding-productivity.env --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import duplicate_work as dw
from lib.config import Config
from lib.storage import get_storage


def _say(msg: str) -> None:
    print(msg, flush=True)


def report(result: dw.Result) -> None:
    counts = result.counts()
    total = sum(counts.values())
    discounted = sum(1 for r in result.moved if r["moved_share"] >= dw.MOVED_DISCOUNT_SHARE)
    _say(f"\nDuplicate-work detection (rules v{dw.DEDUPE_RULES_VERSION})")
    _say(f"  Commits analysed:        {result.commits:,}")
    _say(f"  Excluded commits:        {total:,} "
         f"(merge {counts['merge']:,} / squash {counts['squash']:,} / "
         f"promotion {counts['promotion']:,} / copy {counts['copy']:,})")
    _say(f"  Files with moved code:   {len(result.moved):,} "
         f"({discounted:,} discounted, moved share >= {dw.MOVED_DISCOUNT_SHARE:g})")
    if result.merged_mrs == 0:
        _say("\nNote: no merged MRs/PRs in storage, so the squash and promotion rules "
             "found nothing.")
    elif result.merged_mrs_with_sha < result.merged_mrs:
        missing = result.merged_mrs - result.merged_mrs_with_sha
        _say(f"\nNote: {missing:,} of {result.merged_mrs:,} merged MRs/PRs carry no "
             "merge/squash commit sha (extracted before v0.14), so the squash and "
             "promotion rules cannot see them. Re-extract the period "
             "(`extract --since ... --until ...`, plus `--full` on GitHub) to fill "
             "the shas, then run dedupe again.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Detect duplicate work.")
    parser.add_argument("--config", "-c", default=None,
                        help="Path to .coding-productivity.env (auto-detected if omitted).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute and report, but do not write any table.")
    args = parser.parse_args(argv)

    config = Config(args.config) if args.config else Config()
    errors = config.validate()
    if errors:
        for e in errors:
            print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    storage = get_storage(config)
    try:
        try:
            storage.create_tables()  # adds the new tables / merge_requests columns
        except RuntimeError:  # read-only storage: fine for a dry run on a current schema
            if not args.dry_run:
                raise
        _say("Scanning commits, merge requests and diffs...")
        result = dw.compute(storage, progress=_say)
        report(result)
        if args.dry_run:
            _say("\nDry run: nothing written.")
        else:
            dw.write(storage, result)
            _say("\nWrote duplicate_commits, file_moved_share and dedupe_runs.")
    finally:
        storage.close()


if __name__ == "__main__":
    main()
