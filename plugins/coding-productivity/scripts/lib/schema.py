"""
Centralized table schemas for the coding-productivity plugin.

Each schema is a plain dict mapping column names to DuckDB-compatible types.
Helper functions generate DDL and column lists from these definitions.
"""

from __future__ import annotations

import re
from collections import OrderedDict

# ── Schema definitions ───────────────────────────────────────────────────────

SCHEMAS: dict[str, OrderedDict[str, str]] = {
    "commits": OrderedDict(
        [
            ("commit_sha", "VARCHAR"),
            ("project_id", "VARCHAR"),
            ("project_name", "VARCHAR"),
            ("project_path", "VARCHAR"),
            ("author_name", "VARCHAR"),
            ("author_email", "VARCHAR"),
            ("committer_name", "VARCHAR"),
            ("committer_email", "VARCHAR"),
            ("authored_date", "TIMESTAMP"),
            ("committed_date", "TIMESTAMP"),
            ("created_at", "TIMESTAMP"),
            ("branch_name", "VARCHAR"),
            ("title", "VARCHAR"),
            ("message", "TEXT"),
            ("additions", "INTEGER"),
            ("deletions", "INTEGER"),
            ("total_changes", "INTEGER"),
            ("parent_ids", "VARCHAR"),
            ("web_url", "VARCHAR"),
            ("extracted_at", "TIMESTAMP"),
        ]
    ),
    "diffs": OrderedDict(
        [
            ("commit_sha", "VARCHAR"),
            ("file_path", "VARCHAR"),
            ("old_path", "VARCHAR"),
            ("new_path", "VARCHAR"),
            ("diff", "TEXT"),
            ("new_file", "BOOLEAN"),
            ("renamed_file", "BOOLEAN"),
            ("deleted_file", "BOOLEAN"),
            ("additions", "INTEGER"),
            ("deletions", "INTEGER"),
            ("extracted_at", "TIMESTAMP"),
        ]
    ),
    "merge_requests": OrderedDict(
        [
            ("mr_id", "VARCHAR"),
            ("mr_iid", "VARCHAR"),
            ("project_id", "VARCHAR"),
            ("project_name", "VARCHAR"),
            ("title", "VARCHAR"),
            ("description", "TEXT"),
            ("state", "VARCHAR"),
            ("author_name", "VARCHAR"),
            ("author_email", "VARCHAR"),
            ("created_at", "TIMESTAMP"),
            ("updated_at", "TIMESTAMP"),
            ("merged_at", "TIMESTAMP"),
            ("closed_at", "TIMESTAMP"),
            ("source_branch", "VARCHAR"),
            ("target_branch", "VARCHAR"),
            ("additions", "INTEGER"),
            ("deletions", "INTEGER"),
            ("web_url", "VARCHAR"),
            ("extracted_at", "TIMESTAMP"),
        ]
    ),
    "mr_commits": OrderedDict(
        [
            ("mr_id", "VARCHAR"),
            ("mr_iid", "VARCHAR"),
            ("commit_sha", "VARCHAR"),
            ("project_id", "VARCHAR"),
            ("extracted_at", "TIMESTAMP"),
        ]
    ),
    "commit_scores": OrderedDict(
        [
            ("commit_sha", "VARCHAR"),
            ("total_productivity", "DOUBLE"),
            ("weighted_score", "DOUBLE"),
            ("overall_category", "VARCHAR"),
            ("scored_at", "TIMESTAMP"),
            # Provenance: which instrument produced this row.
            ("scorer_model", "VARCHAR"),
            ("prompt_version", "VARCHAR"),
        ]
    ),
    "file_scores": OrderedDict(
        [
            ("commit_sha", "VARCHAR"),
            ("file_path", "VARCHAR"),
            ("score", "DOUBLE"),
            ("category", "VARCHAR"),
            ("reasoning", "TEXT"),
            ("scored_at", "TIMESTAMP"),
            ("scorer_model", "VARCHAR"),
            ("prompt_version", "VARCHAR"),
        ]
    ),
    # Append-only log of scoring series (see lib/scoring_meta.py). The active
    # series is the most recent row with status = 'active'.
    "scoring_meta": OrderedDict(
        [
            ("version", "INTEGER"),
            ("status", "VARCHAR"),  # 'building' | 'active'
            ("commit_table", "VARCHAR"),
            ("file_table", "VARCHAR"),
            ("scorer_model", "VARCHAR"),  # the configured SCORING_MODEL
            ("prompt_version", "VARCHAR"),
            ("recorded_at", "TIMESTAMP"),
        ]
    ),
}

# Versioned score tables (``commit_scores_v2``, ``file_scores_v2``, ...) share
# the schema of their unversioned base table.
_VERSIONED_RE = re.compile(r"^(commit_scores|file_scores)_v([0-9]+)$")


# ── Public helpers ───────────────────────────────────────────────────────────

def base_table(table_name: str) -> str:
    """
    Resolve *table_name* to the key of its schema in ``SCHEMAS``.

    Versioned score tables map to their base table. Anything else must be a
    known table, so this doubles as the whitelist that makes it safe to
    interpolate a table name into SQL (identifiers cannot be bound parameters).
    """
    if table_name in SCHEMAS:
        return table_name
    m = _VERSIONED_RE.match(table_name)
    if m:
        return m.group(1)
    raise ValueError(f"Unknown table: {table_name}")


def versioned_score_tables(names) -> set[str]:
    """Filter *names* down to versioned score tables (``commit_scores_v2``...)."""
    return {n for n in names if _VERSIONED_RE.match(n)}


def get_columns(table_name: str) -> list[str]:
    """Return the ordered list of column names for *table_name*."""
    return list(SCHEMAS[base_table(table_name)].keys())


def get_column_types(table_name: str) -> "OrderedDict[str, str]":
    """Return the ``column -> DuckDB type`` mapping for *table_name*."""
    return SCHEMAS[base_table(table_name)]


def get_create_sql(table_name: str) -> str:
    """
    Generate a ``CREATE TABLE IF NOT EXISTS`` DDL statement for *table_name*.

    Uses DuckDB-compatible types.
    """
    columns = get_column_types(table_name)
    col_defs = ",\n    ".join(f"{col} {dtype}" for col, dtype in columns.items())
    return f"CREATE TABLE IF NOT EXISTS {table_name} (\n    {col_defs}\n);"
