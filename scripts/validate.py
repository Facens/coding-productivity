#!/usr/bin/env python3
"""
Self-testing validation framework for the coding-productivity plugin.

Compares plugin analysis results against a reference dataset to verify
correctness. Supports CSV and JSON reference files with configurable
tolerance thresholds for floating-point metrics.

Usage:
    python3 validate.py --config .coding-productivity.env \
        --reference reference-2026-01-31.json \
        --since 2026-01-01 --until 2026-01-31

    python3 validate.py --config .coding-productivity.env \
        --reference reference.csv \
        --since 2026-01-01 --until 2026-01-31 --tolerance 0.05
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Ensure the lib package is importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import Config
from lib.storage import get_storage

# Re-use the query functions from analyze.py.
from analyze import monthly_trends, author_productivity, category_distribution


# ── Reference data loading ───────────────────────────────────────────────────

def _load_json_reference(path: Path) -> dict:
    """Load a JSON reference file."""
    with open(path) as f:
        return json.load(f)


def _load_csv_reference(path: Path) -> dict:
    """Load a CSV reference file and convert to the canonical JSON structure.

    The CSV must have a 'dataset' column indicating which dataset each row
    belongs to (monthly_trends, author_productivity, category_distribution).
    Numeric columns are auto-converted.
    """
    rows: list[dict[str, Any]] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            converted: dict[str, Any] = {}
            for key, value in row.items():
                converted[key] = _auto_convert(value)
            rows.append(converted)

    # Group rows by dataset.
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        dataset = str(row.pop("dataset", "unknown"))
        grouped.setdefault(dataset, []).append(row)

    result: dict[str, Any] = {}
    for dataset_name, dataset_rows in grouped.items():
        result[dataset_name] = dataset_rows

    return result


def _auto_convert(value: str) -> Any:
    """Try to convert a string to int or float, falling back to str."""
    if value == "" or value is None:
        return None
    try:
        iv = int(value)
        # Only return int if the string had no decimal point.
        if "." not in value:
            return iv
    except (ValueError, TypeError):
        pass
    try:
        return float(value)
    except (ValueError, TypeError):
        pass
    return value


def load_reference(path: Path) -> dict:
    """Auto-detect format and load a reference file."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        return _load_json_reference(path)
    if suffix == ".csv":
        return _load_csv_reference(path)
    raise ValueError(
        f"Unsupported reference file format: '{suffix}'. Use .json or .csv."
    )


# ── Comparison logic ─────────────────────────────────────────────────────────

class ComparisonResult:
    """Result of comparing a single metric."""

    def __init__(self, metric: str, reference: Any, plugin: Any,
                 status: str, delta: Any = None):
        self.metric = metric
        self.reference = reference
        self.plugin = plugin
        self.status = status  # PASS, OK, FAIL
        self.delta = delta

    def __repr__(self) -> str:
        return f"<{self.status}: {self.metric} ref={self.reference} got={self.plugin}>"


def _normalize_value(value: Any) -> Any:
    """Normalize a value for comparison (handle datetime, Decimal, etc.)."""
    if isinstance(value, (datetime, date)):
        return str(value)[:7]  # YYYY-MM for month grouping
    if value is None:
        return None
    return value


def _is_integer_metric(name: str) -> bool:
    """Determine if a metric should use exact integer comparison."""
    integer_keywords = (
        "count", "commits", "unique_authors", "commit_count",
        "authors", "merged_count", "total_mrs",
    )
    return any(kw in name.lower() for kw in integer_keywords)


def _is_ranking_metric(name: str) -> bool:
    """Determine if a metric represents a ranking/order."""
    return name.lower() in ("author", "author_email", "author_name", "category")


def compare_scalar(metric: str, reference: Any, plugin: Any,
                   tolerance: float) -> ComparisonResult:
    """Compare two scalar values with tolerance for floats."""
    ref = _normalize_value(reference)
    got = _normalize_value(plugin)

    # Both None.
    if ref is None and got is None:
        return ComparisonResult(metric, ref, got, "PASS", 0)

    # One is None.
    if ref is None or got is None:
        return ComparisonResult(metric, ref, got, "FAIL", None)

    # String comparison (categories, months, names).
    if isinstance(ref, str) or isinstance(got, str):
        status = "PASS" if str(ref) == str(got) else "FAIL"
        return ComparisonResult(metric, ref, got, status, None)

    # Numeric comparison.
    try:
        ref_num = float(ref)
        got_num = float(got)
    except (ValueError, TypeError):
        status = "PASS" if ref == got else "FAIL"
        return ComparisonResult(metric, ref, got, status, None)

    delta = got_num - ref_num

    if _is_integer_metric(metric):
        # Integer metrics: exact match required.
        if int(ref_num) == int(got_num):
            return ComparisonResult(metric, ref, got, "PASS", 0)
        return ComparisonResult(metric, ref, got, "FAIL", delta)

    # Float metrics: within tolerance.
    if abs(delta) <= tolerance:
        status = "PASS" if delta == 0 else "OK"
        return ComparisonResult(metric, ref, got, status, round(delta, 6))

    return ComparisonResult(metric, ref, got, "FAIL", round(delta, 6))


def compare_ranking(ref_order: list[str], plugin_order: list[str],
                    metric_label: str, top_n: int | None = None) -> ComparisonResult:
    """Compare ranking order for top N entries."""
    n = top_n or min(len(ref_order), len(plugin_order))
    ref_top = ref_order[:n]
    plugin_top = plugin_order[:n]

    if ref_top == plugin_top:
        return ComparisonResult(
            f"{metric_label} (top {n} order)", str(ref_top), str(plugin_top),
            "PASS", None,
        )
    return ComparisonResult(
        f"{metric_label} (top {n} order)", str(ref_top), str(plugin_top),
        "FAIL", None,
    )


# ── Dataset comparison functions ─────────────────────────────────────────────

def _match_month(ref_month: str, plugin_row: dict) -> bool:
    """Check whether a plugin row matches a reference month string."""
    plugin_month = plugin_row.get("month")
    if plugin_month is None:
        return False
    return str(plugin_month)[:7] == str(ref_month)[:7]


def compare_monthly_trends(ref_rows: list[dict], plugin_rows: list[dict],
                           tolerance: float) -> list[ComparisonResult]:
    """Compare monthly_trends datasets."""
    results: list[ComparisonResult] = []
    metrics = ("commit_count", "commits", "unique_authors",
               "total_productivity", "avg_score", "avg_weighted_score")

    for ref_row in ref_rows:
        ref_month = str(ref_row.get("month", ""))[:7]
        # Find matching plugin row.
        match = None
        for pr in plugin_rows:
            if _match_month(ref_month, pr):
                match = pr
                break

        if match is None:
            for m in metrics:
                if m in ref_row:
                    results.append(ComparisonResult(
                        f"monthly_trends[{ref_month}].{m}",
                        ref_row[m], None, "FAIL", None,
                    ))
            continue

        # Map reference field names to plugin field names (handle aliases).
        field_map = {
            "commit_count": "commits",
            "avg_score": "avg_weighted_score",
        }

        for m in metrics:
            if m not in ref_row:
                continue
            plugin_key = field_map.get(m, m)
            ref_val = ref_row[m]
            plugin_val = match.get(plugin_key, match.get(m))
            result = compare_scalar(
                f"monthly_trends[{ref_month}].{m}",
                ref_val, plugin_val, tolerance,
            )
            results.append(result)

    return results


def compare_author_productivity(ref_rows: list[dict], plugin_rows: list[dict],
                                tolerance: float) -> list[ComparisonResult]:
    """Compare author_productivity datasets."""
    results: list[ComparisonResult] = []

    # Compare ranking order using the identifier field.
    id_field = "author"
    if ref_rows and "author_email" in ref_rows[0]:
        id_field = "author_email"

    ref_order = [str(r.get(id_field, r.get("author_email", ""))) for r in ref_rows]

    plugin_id_field = "author_email"
    plugin_order = [str(r.get(plugin_id_field, "")) for r in plugin_rows]

    top_n = min(len(ref_order), len(plugin_order), 10)
    results.append(compare_ranking(ref_order, plugin_order, "author_ranking", top_n))

    # Compare per-author metrics.
    plugin_by_id: dict[str, dict] = {}
    for pr in plugin_rows:
        key = str(pr.get("author_email", ""))
        plugin_by_id[key] = pr

    for ref_row in ref_rows:
        author_id = str(ref_row.get(id_field, ref_row.get("author_email", "")))
        match = plugin_by_id.get(author_id)

        # Compare commits.
        if "commits" in ref_row:
            ref_val = ref_row["commits"]
            plugin_val = match.get("commits") if match else None
            results.append(compare_scalar(
                f"author[{author_id}].commits",
                ref_val, plugin_val, tolerance,
            ))

        # Compare total_productivity.
        if "total_productivity" in ref_row:
            ref_val = ref_row["total_productivity"]
            plugin_val = match.get("total_productivity") if match else None
            results.append(compare_scalar(
                f"author[{author_id}].total_productivity",
                ref_val, plugin_val, tolerance,
            ))

    return results


def compare_category_distribution(ref_rows: list[dict], plugin_rows: list[dict],
                                  tolerance: float) -> list[ComparisonResult]:
    """Compare category_distribution datasets."""
    results: list[ComparisonResult] = []

    # Build lookup by category name.
    plugin_by_cat: dict[str, dict] = {}
    for pr in plugin_rows:
        cat = str(pr.get("category", ""))
        plugin_by_cat[cat] = pr

    for ref_row in ref_rows:
        cat = str(ref_row.get("category", ""))
        match = plugin_by_cat.get(cat)

        # Count (integer metric).
        if "count" in ref_row:
            plugin_key = "commit_count"  # plugin uses commit_count
            plugin_val = match.get(plugin_key, match.get("count")) if match else None
            results.append(compare_scalar(
                f"category[{cat}].count",
                ref_row["count"], plugin_val, tolerance,
            ))

        # Percentage (float metric).
        if "percentage" in ref_row:
            plugin_key = "pct_of_total"  # plugin uses pct_of_total
            plugin_val = match.get(plugin_key, match.get("percentage")) if match else None
            results.append(compare_scalar(
                f"category[{cat}].percentage",
                ref_row["percentage"], plugin_val, tolerance,
            ))

    return results


# ── Reporting ────────────────────────────────────────────────────────────────

def _trunc(value: Any, max_len: int = 40) -> str:
    """Truncate a display value for table output."""
    s = str(value) if value is not None else "-"
    return s[:max_len] + "..." if len(s) > max_len else s


def format_results_table(results: list[ComparisonResult]) -> str:
    """Format comparison results as a markdown table."""
    if not results:
        return "_No comparisons performed._\n"

    # Calculate column widths.
    headers = ["Metric", "Reference", "Plugin", "Status", "Delta"]
    rows_data: list[list[str]] = []
    for r in results:
        rows_data.append([
            _trunc(r.metric, 50),
            _trunc(r.reference, 25),
            _trunc(r.plugin, 25),
            r.status,
            _trunc(r.delta) if r.delta is not None else "-",
        ])

    widths = [len(h) for h in headers]
    for row in rows_data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Build table.
    def _row(cells: list[str]) -> str:
        padded = [cells[i].ljust(widths[i]) for i in range(len(cells))]
        return "| " + " | ".join(padded) + " |"

    lines = [
        _row(headers),
        "| " + " | ".join("-" * w for w in widths) + " |",
    ]
    for row in rows_data:
        lines.append(_row(row))

    return "\n".join(lines) + "\n"


def format_summary(results: list[ComparisonResult]) -> str:
    """Generate the summary line."""
    counts = {"PASS": 0, "OK": 0, "FAIL": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return f"Validation: {counts['PASS']} PASS, {counts['OK']} OK, {counts['FAIL']} FAIL"


# ── Main ─────────────────────────────────────────────────────────────────────

def run_validation(config_path: str, reference_path: str,
                   since: str, until: str,
                   tolerance: float = 0.01) -> tuple[list[ComparisonResult], int]:
    """Run the full validation pipeline.

    Returns (results, exit_code).
    """
    config = Config(config_path)
    storage = get_storage(config)

    try:
        ref_data = load_reference(Path(reference_path))

        # Use period from reference file if available and not overridden.
        period = ref_data.get("period", {})
        effective_since = since or period.get("since", "")
        effective_until = until or period.get("until", "")

        if not effective_since or not effective_until:
            print("Error: --since and --until are required (or set period in reference file).",
                  file=sys.stderr)
            return [], 1

        all_results: list[ComparisonResult] = []

        # Monthly trends.
        if "monthly_trends" in ref_data:
            plugin_rows = monthly_trends(storage, config,
                                         effective_since, effective_until)
            results = compare_monthly_trends(
                ref_data["monthly_trends"], plugin_rows, tolerance,
            )
            all_results.extend(results)

        # Author productivity.
        if "author_productivity" in ref_data:
            plugin_rows = author_productivity(storage, config,
                                              effective_since, effective_until)
            results = compare_author_productivity(
                ref_data["author_productivity"], plugin_rows, tolerance,
            )
            all_results.extend(results)

        # Category distribution.
        if "category_distribution" in ref_data:
            plugin_rows = category_distribution(storage, config,
                                                effective_since, effective_until)
            results = compare_category_distribution(
                ref_data["category_distribution"], plugin_rows, tolerance,
            )
            all_results.extend(results)

        # Print results.
        print(format_results_table(all_results))
        summary = format_summary(all_results)
        print(summary)

        exit_code = 1 if any(r.status == "FAIL" for r in all_results) else 0
        return all_results, exit_code

    finally:
        storage.close()


def main():
    parser = argparse.ArgumentParser(
        description="Validate plugin analysis results against a reference dataset.",
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to .coding-productivity.env config file.",
    )
    parser.add_argument(
        "--reference", required=True,
        help="Path to reference data file (CSV or JSON).",
    )
    parser.add_argument(
        "--since",
        help="Start date (YYYY-MM-DD). Overrides period in reference file.",
    )
    parser.add_argument(
        "--until",
        help="End date (YYYY-MM-DD). Overrides period in reference file.",
    )
    parser.add_argument(
        "--tolerance", type=float, default=0.01,
        help="Tolerance for float metric comparisons (default: 0.01).",
    )

    args = parser.parse_args()

    _, exit_code = run_validation(
        config_path=args.config,
        reference_path=args.reference,
        since=args.since or "",
        until=args.until or "",
        tolerance=args.tolerance,
    )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
