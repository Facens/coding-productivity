"""
Centralized table schemas for the coding-productivity plugin.

Each schema is a plain dict mapping column names to DuckDB-compatible types.
Helper functions generate DDL and column lists from these definitions.
"""

from __future__ import annotations

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
        ]
    ),
}


# ── Public helpers ───────────────────────────────────────────────────────────

def get_columns(table_name: str) -> list[str]:
    """Return the ordered list of column names for *table_name*."""
    if table_name not in SCHEMAS:
        raise ValueError(f"Unknown table: {table_name}")
    return list(SCHEMAS[table_name].keys())


def get_create_sql(table_name: str) -> str:
    """
    Generate a ``CREATE TABLE IF NOT EXISTS`` DDL statement for *table_name*.

    Uses DuckDB-compatible types.
    """
    if table_name not in SCHEMAS:
        raise ValueError(f"Unknown table: {table_name}")

    columns = SCHEMAS[table_name]
    col_defs = ",\n    ".join(f"{col} {dtype}" for col, dtype in columns.items())
    return f"CREATE TABLE IF NOT EXISTS {table_name} (\n    {col_defs}\n);"
