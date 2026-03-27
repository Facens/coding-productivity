#!/usr/bin/env python3
"""
Analysis engine for the coding-productivity plugin.

Runs DuckDB-compatible SQL queries against the shared storage abstraction
to produce productivity metrics: monthly trends, period comparisons,
per-author stats, merge velocity, category distribution, and code efficiency.

Usage:
    python3 analyze.py --config .coding-productivity.env \
        --query monthly_trends --since 2025-01-01 --until 2025-12-31

    python3 analyze.py --config .coding-productivity.env \
        --query period_comparison \
        --baseline-start 2025-01-01 --baseline-end 2025-06-30 \
        --comparison-start 2025-07-01 --comparison-end 2025-12-31

    python3 analyze.py --config .coding-productivity.env \
        --query all --since 2025-01-01 --until 2025-12-31 --format csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import date, datetime
from pathlib import Path

# Ensure the lib package is importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import Config
from lib.storage import get_storage, Storage


# ── Helpers ──────────────────────────────────────────────────────────────────

def _repo_filter(config: Config) -> tuple[str, dict]:
    """Build a WHERE clause fragment and params dict to filter by REPOS.

    Returns ("", {}) when no repos are configured (match everything).
    """
    repos = config.REPOS
    if not repos:
        return "", {}
    placeholders = ", ".join(f"$repo_{i}" for i in range(len(repos)))
    clause = f"c.project_path IN ({placeholders})"
    params = {f"repo_{i}": r for i, r in enumerate(repos)}
    return clause, params


def _excluded_devs_filter(config: Config) -> tuple[str, dict]:
    """Build a WHERE clause fragment to exclude developers."""
    excluded = config.EXCLUDED_DEVELOPERS
    if not excluded:
        return "", {}
    placeholders = ", ".join(f"$excl_{i}" for i in range(len(excluded)))
    clause = f"c.author_email NOT IN ({placeholders})"
    params = {f"excl_{i}": e for i, e in enumerate(excluded)}
    return clause, params


def _build_where(config: Config, since: str, until: str,
                 extra_clauses: list[str] | None = None,
                 date_col: str = "c.authored_date") -> tuple[str, dict]:
    """Combine repo, exclusion, and date filters into a single WHERE clause."""
    clauses: list[str] = []
    params: dict = {}

    clauses.append(f"{date_col} >= $since")
    params["since"] = since

    clauses.append(f"{date_col} < $until")
    params["until"] = until

    repo_clause, repo_params = _repo_filter(config)
    if repo_clause:
        clauses.append(repo_clause)
        params.update(repo_params)

    excl_clause, excl_params = _excluded_devs_filter(config)
    if excl_clause:
        clauses.append(excl_clause)
        params.update(excl_params)

    if extra_clauses:
        clauses.extend(extra_clauses)

    return "WHERE " + " AND ".join(clauses), params


def _has_scores(storage: Storage) -> bool:
    """Check whether the commit_scores table has any data."""
    try:
        rows = storage.query("SELECT COUNT(*) AS cnt FROM commit_scores")
        return rows[0]["cnt"] > 0 if rows else False
    except Exception:
        return False


# ── Query functions ──────────────────────────────────────────────────────────

def monthly_trends(storage: Storage, config: Config,
                   since: str, until: str) -> list[dict]:
    """Monthly commit count, total productivity, avg weighted score, unique authors."""
    scored = _has_scores(storage)
    where, params = _build_where(config, since, until)

    if scored:
        sql = f"""
        SELECT
            date_trunc('month', c.authored_date) AS month,
            COUNT(DISTINCT c.commit_sha) AS commits,
            COUNT(DISTINCT c.author_email) AS unique_authors,
            ROUND(COALESCE(SUM(s.total_productivity), 0), 2) AS total_productivity,
            ROUND(COALESCE(AVG(s.weighted_score), 0), 3) AS avg_weighted_score
        FROM commits c
        LEFT JOIN commit_scores s ON c.commit_sha = s.commit_sha
        {where}
        GROUP BY date_trunc('month', c.authored_date)
        ORDER BY month
        """
    else:
        sql = f"""
        SELECT
            date_trunc('month', c.authored_date) AS month,
            COUNT(DISTINCT c.commit_sha) AS commits,
            COUNT(DISTINCT c.author_email) AS unique_authors,
            SUM(c.additions + c.deletions) AS total_lines_changed
        FROM commits c
        {where}
        GROUP BY date_trunc('month', c.authored_date)
        ORDER BY month
        """

    return storage.query(sql, params)


def period_comparison(storage: Storage, config: Config,
                      baseline_start: str, baseline_end: str,
                      comparison_start: str, comparison_end: str) -> list[dict]:
    """Before/after metrics with percentage change."""
    scored = _has_scores(storage)

    # Build repo and exclusion filters once.
    repo_clause, repo_params = _repo_filter(config)
    excl_clause, excl_params = _excluded_devs_filter(config)

    extra = []
    if repo_clause:
        extra.append(repo_clause)
    if excl_clause:
        extra.append(excl_clause)

    extra_sql = (" AND " + " AND ".join(extra)) if extra else ""
    params = {
        "b_start": baseline_start,
        "b_end": baseline_end,
        "c_start": comparison_start,
        "c_end": comparison_end,
    }
    params.update(repo_params)
    params.update(excl_params)

    if scored:
        sql = f"""
        WITH baseline AS (
            SELECT
                COUNT(DISTINCT c.commit_sha) AS commits,
                COUNT(DISTINCT c.author_email) AS unique_authors,
                ROUND(COALESCE(SUM(s.total_productivity), 0), 2) AS total_productivity,
                ROUND(COALESCE(AVG(s.weighted_score), 0), 3) AS avg_weighted_score,
                COUNT(DISTINCT CAST(c.authored_date AS DATE)) AS active_days
            FROM commits c
            LEFT JOIN commit_scores s ON c.commit_sha = s.commit_sha
            WHERE c.authored_date >= $b_start AND c.authored_date < $b_end
                  {extra_sql}
        ),
        comparison AS (
            SELECT
                COUNT(DISTINCT c.commit_sha) AS commits,
                COUNT(DISTINCT c.author_email) AS unique_authors,
                ROUND(COALESCE(SUM(s.total_productivity), 0), 2) AS total_productivity,
                ROUND(COALESCE(AVG(s.weighted_score), 0), 3) AS avg_weighted_score,
                COUNT(DISTINCT CAST(c.authored_date AS DATE)) AS active_days
            FROM commits c
            LEFT JOIN commit_scores s ON c.commit_sha = s.commit_sha
            WHERE c.authored_date >= $c_start AND c.authored_date < $c_end
                  {extra_sql}
        )
        SELECT
            'baseline' AS period,
            b.commits, b.unique_authors, b.total_productivity,
            b.avg_weighted_score, b.active_days
        FROM baseline b
        UNION ALL
        SELECT
            'comparison' AS period,
            co.commits, co.unique_authors, co.total_productivity,
            co.avg_weighted_score, co.active_days
        FROM comparison co
        """
    else:
        sql = f"""
        WITH baseline AS (
            SELECT
                COUNT(DISTINCT c.commit_sha) AS commits,
                COUNT(DISTINCT c.author_email) AS unique_authors,
                SUM(c.additions + c.deletions) AS total_lines_changed,
                COUNT(DISTINCT CAST(c.authored_date AS DATE)) AS active_days
            FROM commits c
            WHERE c.authored_date >= $b_start AND c.authored_date < $b_end
                  {extra_sql}
        ),
        comparison AS (
            SELECT
                COUNT(DISTINCT c.commit_sha) AS commits,
                COUNT(DISTINCT c.author_email) AS unique_authors,
                SUM(c.additions + c.deletions) AS total_lines_changed,
                COUNT(DISTINCT CAST(c.authored_date AS DATE)) AS active_days
            FROM commits c
            WHERE c.authored_date >= $c_start AND c.authored_date < $c_end
                  {extra_sql}
        )
        SELECT
            'baseline' AS period,
            b.commits, b.unique_authors, b.total_lines_changed, b.active_days
        FROM baseline b
        UNION ALL
        SELECT
            'comparison' AS period,
            co.commits, co.unique_authors, co.total_lines_changed, co.active_days
        FROM comparison co
        """

    rows = storage.query(sql, params)

    # Compute percentage change and append.
    if len(rows) == 2:
        b, c = rows[0], rows[1]
        change_row = {"period": "% change"}
        for key in b:
            if key == "period":
                continue
            bv = b[key] or 0
            cv = c[key] or 0
            if bv != 0:
                change_row[key] = round((cv - bv) / bv * 100, 1)
            else:
                change_row[key] = None
        rows.append(change_row)

    return rows


def author_productivity(storage: Storage, config: Config,
                        since: str, until: str, limit: int = 20) -> list[dict]:
    """Per-author: commits, total productivity, avg score, top category."""
    scored = _has_scores(storage)
    where, params = _build_where(config, since, until)
    params["lim"] = limit

    if scored:
        sql = f"""
        WITH author_stats AS (
            SELECT
                c.author_name,
                c.author_email,
                COUNT(DISTINCT c.commit_sha) AS commits,
                ROUND(COALESCE(SUM(s.total_productivity), 0), 2) AS total_productivity,
                ROUND(COALESCE(AVG(s.weighted_score), 0), 3) AS avg_weighted_score
            FROM commits c
            LEFT JOIN commit_scores s ON c.commit_sha = s.commit_sha
            {where}
            GROUP BY c.author_name, c.author_email
        ),
        top_cats AS (
            SELECT
                c.author_email,
                s.overall_category,
                COUNT(*) AS cat_count,
                ROW_NUMBER() OVER (
                    PARTITION BY c.author_email ORDER BY COUNT(*) DESC
                ) AS rn
            FROM commits c
            JOIN commit_scores s ON c.commit_sha = s.commit_sha
            {where}
            GROUP BY c.author_email, s.overall_category
        )
        SELECT
            a.author_name,
            a.author_email,
            a.commits,
            a.total_productivity,
            a.avg_weighted_score,
            COALESCE(tc.overall_category, '-') AS top_category
        FROM author_stats a
        LEFT JOIN top_cats tc
            ON a.author_email = tc.author_email AND tc.rn = 1
        ORDER BY a.total_productivity DESC
        LIMIT $lim
        """
    else:
        sql = f"""
        SELECT
            c.author_name,
            c.author_email,
            COUNT(DISTINCT c.commit_sha) AS commits,
            SUM(c.additions + c.deletions) AS total_lines_changed
        FROM commits c
        {where}
        GROUP BY c.author_name, c.author_email
        ORDER BY total_lines_changed DESC
        LIMIT $lim
        """

    return storage.query(sql, params)


def merge_velocity(storage: Storage, config: Config,
                   since: str, until: str) -> list[dict]:
    """Monthly: total MRs, merged count, merge rate %, avg time to merge (days),
    velocity at 7d and 30d windows."""
    # Use merge_requests table; filter by created_at.
    repo_clause, repo_params = _repo_filter(config)
    excl_clause, excl_params = _excluded_devs_filter(config)

    # Adapt filters for MR table aliases (m instead of c).
    # MR project_name stores short names (e.g., "admin"), not full paths
    # (e.g., "team-blue-Hub/admin"). Rebuild repo params with short names.
    if repo_clause:
        mr_repo_params = {k: v.split("/")[-1] if "/" in v else v
                          for k, v in repo_params.items()}
        mr_repo_clause = repo_clause.replace("c.project_path", "m.project_name")
        repo_params = mr_repo_params  # override for this query
    else:
        mr_repo_clause = ""
    mr_excl_clause = excl_clause.replace("c.author_email", "m.author_email") if excl_clause else ""

    clauses = ["m.created_at >= $since", "m.created_at < $until"]
    if mr_repo_clause:
        clauses.append(mr_repo_clause)
    if mr_excl_clause:
        clauses.append(mr_excl_clause)

    where_sql = "WHERE " + " AND ".join(clauses)

    params = {"since": since, "until": until}
    params.update(repo_params)
    params.update(excl_params)

    sql = f"""
    SELECT
        date_trunc('month', m.created_at) AS month,
        COUNT(*) AS total_mrs,
        COUNT(*) FILTER (WHERE m.state = 'merged') AS merged_count,
        ROUND(
            COUNT(*) FILTER (WHERE m.state = 'merged') * 100.0
            / NULLIF(COUNT(*), 0), 1
        ) AS merge_rate_pct,
        ROUND(
            AVG(
                CASE WHEN m.merged_at IS NOT NULL
                     THEN EXTRACT(EPOCH FROM (m.merged_at - m.created_at)) / 86400.0
                END
            ), 2
        ) AS avg_days_to_merge,
        COUNT(*) FILTER (
            WHERE m.state = 'merged'
              AND m.merged_at IS NOT NULL
              AND EXTRACT(EPOCH FROM (m.merged_at - m.created_at)) / 86400.0 <= 7
        ) AS merged_within_7d,
        COUNT(*) FILTER (
            WHERE m.state = 'merged'
              AND m.merged_at IS NOT NULL
              AND EXTRACT(EPOCH FROM (m.merged_at - m.created_at)) / 86400.0 <= 30
        ) AS merged_within_30d
    FROM merge_requests m
    {where_sql}
    GROUP BY date_trunc('month', m.created_at)
    ORDER BY month
    """

    return storage.query(sql, params)


def category_distribution(storage: Storage, config: Config,
                          since: str, until: str) -> list[dict]:
    """Category, count, % of total, avg score."""
    if not _has_scores(storage):
        return []

    where, params = _build_where(config, since, until)

    sql = f"""
    WITH cats AS (
        SELECT
            s.overall_category AS category,
            COUNT(*) AS commit_count,
            ROUND(AVG(s.weighted_score), 3) AS avg_score
        FROM commits c
        JOIN commit_scores s ON c.commit_sha = s.commit_sha
        {where}
        GROUP BY s.overall_category
    ),
    total AS (
        SELECT SUM(commit_count) AS total_count FROM cats
    )
    SELECT
        cats.category,
        cats.commit_count,
        ROUND(cats.commit_count * 100.0 / NULLIF(total.total_count, 0), 1) AS pct_of_total,
        cats.avg_score
    FROM cats, total
    ORDER BY cats.commit_count DESC
    """

    return storage.query(sql, params)


def code_efficiency(storage: Storage, config: Config,
                    since: str, until: str) -> list[dict]:
    """Monthly: total additions from all commits, additions from merged commits,
    efficiency ratio."""
    repo_clause, repo_params = _repo_filter(config)
    excl_clause, excl_params = _excluded_devs_filter(config)

    clauses = ["c.authored_date >= $since", "c.authored_date < $until"]
    if repo_clause:
        clauses.append(repo_clause)
    if excl_clause:
        clauses.append(excl_clause)
    where_sql = "WHERE " + " AND ".join(clauses)

    params = {"since": since, "until": until}
    params.update(repo_params)
    params.update(excl_params)

    sql = f"""
    WITH commit_merged AS (
        SELECT DISTINCT mc.commit_sha
        FROM mr_commits mc
        JOIN merge_requests mr ON mc.mr_id = mr.mr_id
        WHERE mr.state = 'merged'
    )
    SELECT
        date_trunc('month', c.authored_date) AS month,
        SUM(c.additions) AS total_additions,
        SUM(CASE WHEN cm.commit_sha IS NOT NULL THEN c.additions ELSE 0 END)
            AS merged_additions,
        ROUND(
            SUM(CASE WHEN cm.commit_sha IS NOT NULL THEN c.additions ELSE 0 END)
            * 100.0
            / NULLIF(SUM(c.additions), 0), 1
        ) AS efficiency_pct
    FROM commits c
    LEFT JOIN commit_merged cm ON c.commit_sha = cm.commit_sha
    {where_sql}
    GROUP BY date_trunc('month', c.authored_date)
    ORDER BY month
    """

    return storage.query(sql, params)


# ── Formatters ───────────────────────────────────────────────────────────────

def _stringify(value) -> str:
    """Convert a value to a display string."""
    if value is None:
        return "-"
    if isinstance(value, (datetime, date)):
        return str(value)[:10]
    if isinstance(value, float):
        return f"{value:,.2f}" if abs(value) >= 100 else f"{value}"
    return str(value)


def format_table(rows: list[dict], columns: list[str] | None = None) -> str:
    """Format rows as a markdown table."""
    if not rows:
        return "_No data._\n"

    if columns is None:
        columns = list(rows[0].keys())

    # Header.
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"

    lines = [header, separator]
    for row in rows:
        cells = [_stringify(row.get(c)) for c in columns]
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines) + "\n"


def format_summary(query_name: str, rows: list[dict],
                   columns: list[str] | None = None) -> str:
    """Format a query result with a header and context."""
    titles = {
        "monthly_trends": "Monthly Trends",
        "period_comparison": "Period Comparison (Before / After)",
        "author_productivity": "Author Productivity",
        "merge_velocity": "Merge Velocity",
        "category_distribution": "Category Distribution",
        "code_efficiency": "Code Efficiency",
    }
    title = titles.get(query_name, query_name)
    count = len(rows) if rows else 0
    header = f"## {title}\n\n_{count} row(s)_\n\n"
    return header + format_table(rows, columns)


def format_csv(rows: list[dict]) -> str:
    """Format rows as CSV."""
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def format_json(rows: list[dict]) -> str:
    """Format rows as JSON."""
    def default(obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return str(obj)
    return json.dumps(rows, indent=2, default=default)


# ── Query registry ───────────────────────────────────────────────────────────

QUERIES = {
    "monthly_trends": monthly_trends,
    "period_comparison": period_comparison,
    "author_productivity": author_productivity,
    "merge_velocity": merge_velocity,
    "category_distribution": category_distribution,
    "code_efficiency": code_efficiency,
}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Run productivity analysis queries.",
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to .coding-productivity.env config file.",
    )
    parser.add_argument(
        "--query", required=True,
        choices=list(QUERIES.keys()) + ["all"],
        help="Which analysis to run (or 'all').",
    )
    parser.add_argument("--since", help="Start date (YYYY-MM-DD).")
    parser.add_argument("--until", help="End date (YYYY-MM-DD).")
    parser.add_argument(
        "--format", dest="output_format", default="table",
        choices=["table", "json", "csv"],
        help="Output format (default: table).",
    )
    # Extra args for period_comparison.
    parser.add_argument("--baseline-start", help="Baseline period start (YYYY-MM-DD).")
    parser.add_argument("--baseline-end", help="Baseline period end (YYYY-MM-DD).")
    parser.add_argument("--comparison-start", help="Comparison period start (YYYY-MM-DD).")
    parser.add_argument("--comparison-end", help="Comparison period end (YYYY-MM-DD).")
    parser.add_argument("--limit", type=int, default=20, help="Row limit for author query.")

    args = parser.parse_args()

    config = Config(args.config)
    storage = get_storage(config)

    try:
        # Determine queries to run.
        if args.query == "all":
            query_names = [q for q in QUERIES if q != "period_comparison"]
        else:
            query_names = [args.query]

        for qname in query_names:
            if qname == "period_comparison":
                for arg in ("baseline_start", "baseline_end",
                            "comparison_start", "comparison_end"):
                    if not getattr(args, arg):
                        print(f"Error: --{arg.replace('_', '-')} is required "
                              f"for period_comparison.", file=sys.stderr)
                        sys.exit(1)
                rows = period_comparison(
                    storage, config,
                    args.baseline_start, args.baseline_end,
                    args.comparison_start, args.comparison_end,
                )
            else:
                if not args.since or not args.until:
                    print("Error: --since and --until are required.",
                          file=sys.stderr)
                    sys.exit(1)

                if qname == "author_productivity":
                    rows = author_productivity(
                        storage, config, args.since, args.until,
                        limit=args.limit,
                    )
                else:
                    rows = QUERIES[qname](storage, config, args.since, args.until)

            # Format output.
            if args.output_format == "json":
                print(format_json(rows))
            elif args.output_format == "csv":
                print(format_csv(rows), end="")
            else:
                print(format_summary(qname, rows))

    finally:
        storage.close()


if __name__ == "__main__":
    main()
