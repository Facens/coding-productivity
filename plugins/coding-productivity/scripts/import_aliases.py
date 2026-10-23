#!/usr/bin/env python3
"""
Load an existing alias table into ``IDENTITY_MERGES``.

Reads a CSV with the header ``original_email,canonical_email`` and adds each
pair to ``IDENTITY_MERGES`` in ``.coding-productivity.env`` (the existing
entries stay). ``analyze`` then counts both addresses as one person; stored
data is not rewritten (``/coding-productivity:developers`` does that for
anonymized data).

A pair is not imported when

* both addresses are equal (ignoring case): nothing to merge;
* either is empty, or contains ``,`` or ``:``: ``IDENTITY_MERGES`` is a
  comma-separated list of ``alias:canonical`` pairs, so those characters would
  split the entry and corrupt the setting (refused, reported);
* the alias is already merged into a different address, in the config or
  earlier in the CSV: the first mapping stays (refused, reported).

Valid pairs are always written. The exit code is 1 when any pair was refused,
0 otherwise, 2 for an unreadable file or a missing header. Emails are never
printed, only CSV line numbers.

Usage::

    python import_aliases.py --config .coding-productivity.env --csv aliases.csv
    python import_aliases.py --config .coding-productivity.env --csv aliases.csv --dry-run
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import Config

REQUIRED_COLUMNS = ("original_email", "canonical_email")


@dataclass
class ImportResult:
    """What an import did (or, with ``dry_run``, would do)."""
    merges: dict[str, str]                       # the full IDENTITY_MERGES after the import
    added: int = 0
    equal: int = 0                               # pairs where both emails are the same
    already: int = 0                             # pairs the config already had
    refused: list[tuple[int, str]] = field(default_factory=list)   # (csv line, reason)


def read_rows(path: Path) -> list[tuple[int, str, str]]:
    """``(csv line, original, canonical)`` for every non-blank row.

    Raises ``ValueError`` when the header lacks ``original_email`` or
    ``canonical_email``; columns may come in any order and any case.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None) or []
        names = [h.strip().lower() for h in header]
        missing = [c for c in REQUIRED_COLUMNS if c not in names]
        if missing:
            raise ValueError(
                f"the CSV needs the columns {', '.join(REQUIRED_COLUMNS)}; "
                f"missing: {', '.join(missing)}."
            )
        i_orig, i_canon = names.index(REQUIRED_COLUMNS[0]), names.index(REQUIRED_COLUMNS[1])
        rows = []
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            get = lambda i: row[i].strip() if i < len(row) else ""  # noqa: E731
            rows.append((reader.line_num, get(i_orig), get(i_canon)))
    return rows


def plan_import(rows: list[tuple[int, str, str]], existing: dict[str, str]) -> ImportResult:
    """Apply the rules above to *rows*, on top of the *existing* merges."""
    merges = dict(existing)
    result = ImportResult(merges=merges)
    for line, original, canonical in rows:
        if not original or not canonical:
            result.refused.append((line, "empty email"))
            continue
        if any(ch in value for value in (original, canonical) for ch in ",:"):
            result.refused.append((line, "email contains ',' or ':'"))
            continue
        alias, target = original.lower(), canonical.lower()
        if alias == target:
            result.equal += 1
        elif alias in merges and merges[alias] != target:
            result.refused.append((line, "alias already merged into a different address"))
        elif alias in merges:
            result.already += 1
        else:
            merges[alias] = target
            result.added += 1
    return result


def import_aliases(config: Config, csv_path: Path, dry_run: bool = False) -> ImportResult:
    """Add the pairs of *csv_path* to ``IDENTITY_MERGES`` (not when *dry_run*)."""
    result = plan_import(read_rows(Path(csv_path)), config.IDENTITY_MERGES)
    if result.added and not dry_run:
        config.update("IDENTITY_MERGES", ",".join(f"{a}:{c}" for a, c in result.merges.items()))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Import an alias table into IDENTITY_MERGES.")
    parser.add_argument("--config", required=True, help="Path to .coding-productivity.env.")
    parser.add_argument("--csv", required=True, help="CSV with original_email,canonical_email.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change without writing the config.")
    args = parser.parse_args()

    try:
        result = import_aliases(Config(args.config), Path(args.csv), dry_run=args.dry_run)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    verb = "would add" if args.dry_run else "added"
    print(f"{verb}: {result.added}; already in IDENTITY_MERGES: {result.already}; "
          f"same address on both sides (skipped): {result.equal}; "
          f"refused: {len(result.refused)}", flush=True)
    for line, reason in result.refused:
        print(f"  refused CSV line {line}: {reason}", flush=True)
    print(f"IDENTITY_MERGES now holds {len(result.merges)} pair(s).", flush=True)
    return 1 if result.refused else 0


if __name__ == "__main__":
    sys.exit(main())
