#!/usr/bin/env python3
"""
Analysis engine for the coding-productivity plugin.

Runs SQL queries (DuckDB or BigQuery, see lib/dialect.py) against the shared storage abstraction
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
from typing import Optional

# Ensure the lib package is importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import dataclass, field

from lib import dialect
from lib import duplicate_work as dw
from lib import identity
from lib import scoring_meta
from lib.merge_requests import LATEST_MRS
from lib.config import Config
from lib.storage import get_storage, is_table_missing, Storage


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
    # NULL-safe: "NULL NOT IN (...)" is NULL, which would drop commits with no email.
    clause = f"(c.author_email IS NULL OR c.author_email NOT IN ({placeholders}))"
    params = {f"excl_{i}": e for i, e in enumerate(excluded)}
    return clause, params


def _date_col(config) -> str:
    """SQL expression for the date a commit is bucketed and filtered by.

    ``DATE_FIELD`` selects ``committed_date`` (default) or ``authored_date``.
    A commit with no ``committed_date`` (stores written before the column was
    filled) falls back to its authored date instead of dropping out.
    """
    if (getattr(config, "DATE_FIELD", None) or "committed_date") == "authored_date":
        return "c.authored_date"
    return "COALESCE(c.committed_date, c.authored_date)"


# ── Identity: who counts as one developer ───────────────────────────────────

class _Scope:
    """*config* with *extra* emails added to ``EXCLUDED_DEVELOPERS``.

    Non-person accounts are removed from every query through the filter that
    already drops excluded developers, so no query needs its own clause.
    """

    def __init__(self, config, extra: list[str]):
        self._config = config
        self._extra = extra

    @property
    def EXCLUDED_DEVELOPERS(self) -> list[str]:
        base = list(getattr(self._config, "EXCLUDED_DEVELOPERS", []) or [])
        return base + [e for e in self._extra if e not in base]

    def __getattr__(self, name):
        return getattr(self._config, name)


def load_people(storage: Storage, config: Config) -> identity.People:
    """Resolve the stored identities of the whole dataset (REPOS and excluded
    developers applied, dates not) into people. All dates are used on purpose:
    one person keeps one key across the baseline and the comparison period."""
    clauses: list[str] = []
    params: dict = {}
    for clause, clause_params in (_repo_filter(config), _excluded_devs_filter(config)):
        if clause:
            clauses.append(clause)
            params.update(clause_params)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = storage.query(f"""
        SELECT c.author_name AS author_name, c.author_email AS author_email,
               COUNT(DISTINCT c.commit_sha) AS commits
        FROM commits c {where}
        GROUP BY c.author_name, c.author_email""", params)
    anonymized = bool(getattr(config, "ANONYMIZATION_ENABLED", False))
    return identity.resolve(
        [(r["author_name"], r["author_email"], r["commits"]) for r in rows],
        merges=getattr(config, "IDENTITY_MERGES", None) or {},
        no_merge=getattr(config, "IDENTITY_NO_MERGE", None) or [],
        auto=getattr(config, "IDENTITY_AUTO_MERGE", True),
        key=getattr(config, "PSEUDONYMIZATION_KEY", None) if anonymized else None,
    )


def _scoped(storage: Storage, config: Config,
            people: Optional[identity.People] = None) -> tuple[_Scope, identity.People]:
    """``(config without non-person accounts, people)`` for one query."""
    if people is None:
        people = load_people(storage, config)
    return _Scope(config, people.non_person_emails()), people


def _min_active_commits(config) -> int:
    return max(1, int(getattr(config, "MIN_ACTIVE_COMMITS", 1) or 1))


def identity_notes(storage: Storage, config: Config,
                   people: Optional[identity.People] = None) -> list[str]:
    """Lines listing the automatic merges and excluded non-person accounts."""
    people = people or load_people(storage, config)
    return identity.format_report(
        people, anonymized=bool(getattr(config, "ANONYMIZATION_ENABLED", False)))


def _build_where(config: Config, since: str, until: str,
                 extra_clauses: list[str] | None = None,
                 date_col: Optional[str] = None) -> tuple[str, dict]:
    """Combine repo, exclusion, and date filters into a single WHERE clause."""
    date_col = date_col or _date_col(config)
    clauses: list[str] = []
    params: dict = {}

    clauses.append(f"{date_col} >= {dialect.timestamp_param('$since')}")
    params["since"] = since

    clauses.append(f"{date_col} < {dialect.timestamp_param('$until')}")
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


def _commit_scores_table(storage: Storage) -> str:
    """Name of the active series' commit score table (see lib/scoring_meta.py)."""
    return scoring_meta.get_active(storage).commit_table


def _has_scores(storage: Storage) -> bool:
    """Check whether the active commit score table has any data."""
    try:
        rows = storage.query(
            f"SELECT COUNT(*) AS cnt FROM {_commit_scores_table(storage)}"
        )
        return rows[0]["cnt"] > 0 if rows else False
    except Exception as exc:
        if not is_table_missing(exc):
            raise
        return False


# ── Duplicate work: exclusion and moved-code discount ────────────────────────

@dataclass
class _Source:
    """Where a query reads scores and line counts, with or without dedupe.

    ``scores`` names a table/CTE with ``commit_sha, total_productivity,
    weighted_score, overall_category``. After a dedupe run those are
    recomputed from the active series' file scores times the moved-code
    factor (file rows deduplicated per ``(commit_sha, file_path)`` first, then
    ``score * max(additions, 1) * factor`` as at scoring time); a commit with
    no file rows keeps its stored totals. ``lines_join`` / ``lines_sum`` do
    the same for the unscored lines-changed metric, and ``excl`` holds the
    WHERE clause that drops ``duplicate_commits``.
    """
    ctes: list[str] = field(default_factory=list)
    scores: str = ""
    lines_join: str = ""
    lines_sum: str = "SUM(c.additions + c.deletions)"
    excl: list[str] = field(default_factory=list)


_NOT_DUPLICATE = "c.commit_sha NOT IN (SELECT commit_sha FROM duplicate_commits)"


def _productivity_sql(prefix: str = "") -> tuple[str, str]:
    """``(raw, adjusted)`` productivity over ``dd_files`` rows (columns behind
    *prefix*). Defined once: the ``adj_commit`` CTE applies it and the summary's
    moved-code discount is measured with it."""
    raw = f"{prefix}score * GREATEST({prefix}additions, 1)"
    return raw, f"{raw} * {prefix}factor"


def _lines_sql(prefix: str = "") -> tuple[str, str]:
    """``(raw, adjusted)`` lines changed, the unscored counterpart of
    ``_productivity_sql``: the CTEs apply it and the summary measures with it."""
    raw = f"({prefix}additions + {prefix}deletions)"
    return raw, f"{raw} * {prefix}factor"


def _dedupe_ctes() -> list[str]:
    """``dd_diffs``: one row per (commit, file) with its moved-code factor."""
    # Constants, not user input, so they are written into the SQL.
    thr = float(dw.MOVED_DISCOUNT_SHARE)
    return [f"""dd_diffs AS (
        SELECT d.commit_sha, d.file_path,
               COALESCE(d.additions, 0) AS additions,
               COALESCE(d.deletions, 0) AS deletions,
               CASE WHEN m.moved_share >= {thr} THEN 1 - m.moved_share ELSE 1 END AS factor
        FROM (
            SELECT commit_sha, file_path, additions, deletions,
                   ROW_NUMBER() OVER (PARTITION BY commit_sha, file_path
                                      ORDER BY extracted_at DESC) AS rn
            FROM diffs
        ) d
        LEFT JOIN file_moved_share m
               ON m.commit_sha = d.commit_sha AND m.file_path = d.file_path
        WHERE d.rn = 1
    )"""]


def _dedupe_file_scores_cte(storage: Storage) -> str:
    """``dd_files``: active series' file scores, deduplicated, with factor."""
    file_table = scoring_meta.get_active(storage).file_table
    return f"""dd_files AS (
        SELECT f.commit_sha, f.file_path, f.score,
               d.additions, d.deletions, d.factor
        FROM (
            SELECT commit_sha, file_path, score,
                   ROW_NUMBER() OVER (PARTITION BY commit_sha, file_path
                                      ORDER BY scored_at DESC) AS rn
            FROM {file_table}
        ) f
        JOIN dd_diffs d ON d.commit_sha = f.commit_sha AND d.file_path = f.file_path
        WHERE f.rn = 1
    )"""


def _source(storage: Storage, scored: bool) -> _Source:
    """Scores/lines source for a query. Plain tables until dedupe has run."""
    commit_table = _commit_scores_table(storage)
    if dw.last_run(storage) is None:
        return _Source(scores=commit_table)
    src = _Source(excl=[_NOT_DUPLICATE])
    src.ctes = _dedupe_ctes()
    _, adj_productivity = _productivity_sql()
    _, adj_lines = _lines_sql()
    if scored:
        src.ctes += [
            _dedupe_file_scores_cte(storage),
            f"""adj_commit AS (
        SELECT commit_sha,
               SUM({adj_productivity}) AS productivity,
               SUM({adj_lines}) AS lines
        FROM dd_files GROUP BY commit_sha
    )""",
            f"""adj_scores AS (
        SELECT s.commit_sha,
               COALESCE(a.productivity, s.total_productivity) AS total_productivity,
               CASE WHEN a.commit_sha IS NULL THEN s.weighted_score
                    ELSE a.productivity / NULLIF(a.lines, 0) END AS weighted_score,
               s.overall_category
        FROM {commit_table} s
        LEFT JOIN adj_commit a ON a.commit_sha = s.commit_sha
    )""",
        ]
        src.scores = "adj_scores"
    else:
        src.ctes.append(f"""adj_lines AS (
        SELECT commit_sha, SUM({adj_lines}) AS lines
        FROM dd_diffs GROUP BY commit_sha
    )""")
        src.lines_join = "LEFT JOIN adj_lines al ON al.commit_sha = c.commit_sha"
        src.lines_sum = ("ROUND(SUM(COALESCE(al.lines, c.additions + c.deletions)), 1)")
    return src


def _with(*ctes: str) -> str:
    """``WITH a AS (...), b AS (...)`` from CTE definitions; '' when none."""
    ctes = [c for c in ctes if c]
    return "WITH " + ",\n".join(ctes) + "\n" if ctes else ""


def _cte_prefix(ctes: list[str]) -> str:
    """Opening of a ``WITH`` that goes on with the query's own CTEs."""
    return "WITH " + "".join(c + ",\n" for c in ctes)


def dedupe_summary(storage: Storage, config: Config, since: str, until: str) -> Optional[str]:
    """One line on what dedupe removed from ``[since, until)``; None if it never ran."""
    if dw.last_run(storage) is None:
        return None
    where, params = _build_where(config, since, until)
    by_rule = {r["why"]: r["n"] for r in storage.query(f"""
        SELECT d.why AS why, COUNT(*) AS n
        FROM commits c JOIN duplicate_commits d ON d.commit_sha = c.commit_sha
        {where} GROUP BY d.why""", params)}
    total = sum(by_rule.values())
    detail = " / ".join(f"{rule} {by_rule.get(rule, 0):,}" for rule in dw.RULE_ORDER)

    scored = _has_scores(storage)
    where, params = _build_where(config, since, until, [_NOT_DUPLICATE])
    if scored:
        ctes = _dedupe_ctes() + [_dedupe_file_scores_cte(storage)]
        raw, adj = _productivity_sql("x.")
        noun = "productivity"
        table = "dd_files"
    else:
        ctes = _dedupe_ctes()
        raw, adj = _lines_sql("x.")
        noun = "lines changed"
        table = "dd_diffs"
    rows = storage.query(f"""
        {_with(*ctes)}SELECT SUM({raw}) AS raw_total, SUM({adj}) AS adj_total
        FROM {table} x JOIN commits c ON c.commit_sha = x.commit_sha
        {where}""", params)
    raw_v = (rows[0]["raw_total"] or 0) if rows else 0
    adj_v = (rows[0]["adj_total"] or 0) if rows else 0
    pct = (raw_v - adj_v) / raw_v * 100 if raw_v else 0.0
    return (f"excluded: {total:,} commits ({detail}), "
            f"moved-code discount {pct:.1f}% of {noun}")


class MixedScorerModelError(RuntimeError):
    """The selected date range mixes scores from different scoring models."""


def check_single_scorer_model(storage: Storage, config: Config,
                              ranges: list[tuple[str, str]],
                              allow_legacy_null: bool = False) -> None:
    """Refuse to analyze scores produced by more than one model.

    The scoring model is a measuring instrument: a trend across two models is
    a trend in the instrument. Raises ``MixedScorerModelError`` when the scored
    commits in *ranges* (``[since, until)`` pairs, as used by the queries) have

    * more than one distinct non-NULL ``scorer_model``, or
    * more than one distinct non-NULL ``prompt_version``, or
    * NULL plus non-NULL provenance (legacy rows predate it), unless
      *allow_legacy_null* is set. The first two are never relaxed.
    """
    table = _commit_scores_table(storage)

    date_col = _date_col(config)
    range_sql = " OR ".join(
        f"({date_col} >= {dialect.timestamp_param(f'$since_{i}')} "
        f"AND {date_col} < {dialect.timestamp_param(f'$until_{i}')})"
        for i in range(len(ranges))
    )
    params: dict = {}
    for i, (since, until) in enumerate(ranges):
        params[f"since_{i}"] = since
        params[f"until_{i}"] = until

    clauses = [f"({range_sql})"]
    repo_clause, repo_params = _repo_filter(config)
    if repo_clause:
        clauses.append(repo_clause)
        params.update(repo_params)
    excl_clause, excl_params = _excluded_devs_filter(config)
    if excl_clause:
        clauses.append(excl_clause)
        params.update(excl_params)

    sql = f"""
    SELECT s.scorer_model AS scorer_model, s.prompt_version AS prompt_version,
           COUNT(*) AS n
    FROM commits c
    JOIN {table} s ON c.commit_sha = s.commit_sha
    WHERE {" AND ".join(clauses)}
    GROUP BY s.scorer_model, s.prompt_version
    """
    try:
        rows = storage.query(sql, params)
    except Exception as exc:
        # A table created before provenance existed has no such columns
        # (e.g. a read-only connection never ran create_tables): all NULL.
        if "scorer_model" not in str(exc) and "prompt_version" not in str(exc):
            raise
        return

    models = sorted({r["scorer_model"] for r in rows if r["scorer_model"] is not None})
    versions = sorted({r["prompt_version"] for r in rows if r["prompt_version"] is not None})
    # A row without a recorded model or prompt_version predates provenance.
    legacy = sum(r["n"] for r in rows
                 if r["scorer_model"] is None or r["prompt_version"] is None)
    recorded = sum(r["n"] for r in rows
                   if r["scorer_model"] is not None and r["prompt_version"] is not None)

    if len(models) > 1:
        raise MixedScorerModelError(
            "The selected date range contains scores from more than one scoring "
            f"model ({', '.join(models)}). A trend across models is a trend in "
            "the instrument, not in productivity. Narrow the range to one model, "
            "or rescore everything with one model: /coding-productivity:calibrate "
            "then /coding-productivity:rebase."
        )
    if len(versions) > 1:
        raise MixedScorerModelError(
            "The selected date range contains scores from more than one "
            f"prompt_version ({', '.join(versions)}): the prompt, effort or scoring "
            "logic changed in between, so the scores are not on one scale. Narrow "
            "the range, or rescore everything: /coding-productivity:rebase."
        )
    if recorded and legacy and not allow_legacy_null:
        raise MixedScorerModelError(
            f"The selected date range mixes {legacy} legacy score(s) with no recorded "
            f"model or prompt_version and {recorded} score(s) from {models[0]} "
            f"(prompt_version {versions[0]}). Legacy rows predate provenance, so the "
            "instrument that produced them is unknown. If you know they came from "
            "the same model and prompt, re-run with --allow-legacy-null; "
            "otherwise rescore with /coding-productivity:rebase."
        )


# ── Query functions ──────────────────────────────────────────────────────────

def _monthly_people(storage: Storage, config: Config, where: str, params: dict,
                    src: _Source, scored: bool) -> list[dict]:
    """Per month and stored author email: commits and productivity (or lines)."""
    if scored:
        value = "COALESCE(SUM(s.total_productivity), 0)"
        join = f"LEFT JOIN {src.scores} s ON c.commit_sha = s.commit_sha"
    else:
        value, join = src.lines_sum, src.lines_join
    month = dialect.month_trunc(storage, _date_col(config))
    return storage.query(f"""
        {_with(*src.ctes)}SELECT
            {month} AS month,
            c.author_email AS author_email,
            COUNT(DISTINCT c.commit_sha) AS commits,
            {value} AS value
        FROM commits c
        {join}
        {where}
        GROUP BY {month}, c.author_email
        """, params)


def monthly_trends(storage: Storage, config: Config,
                   since: str, until: str,
                   people: Optional[identity.People] = None) -> list[dict]:
    """Monthly commit count, total productivity, avg weighted score, developers.

    ``unique_authors`` counts people, not emails (see ``lib/identity.py``).
    With ``MIN_ACTIVE_COMMITS`` above 1 two columns are added: the developers
    with at least that many commits in the month, and the productivity (lines
    changed when unscored) of those developers per developer.
    """
    config, people = _scoped(storage, config, people)
    scored = _has_scores(storage)
    src = _source(storage, scored)
    where, params = _build_where(config, since, until, src.excl)
    month = dialect.month_trunc(storage, _date_col(config))

    if scored:
        sql = f"""
        {_with(*src.ctes)}SELECT
            {month} AS month,
            COUNT(DISTINCT c.commit_sha) AS commits,
            NULL AS unique_authors,
            ROUND(COALESCE(SUM(s.total_productivity), 0), 2) AS total_productivity,
            ROUND(COALESCE(AVG(s.weighted_score), 0), 3) AS avg_weighted_score
        FROM commits c
        LEFT JOIN {src.scores} s ON c.commit_sha = s.commit_sha
        {where}
        GROUP BY {month}
        ORDER BY month
        """
    else:
        sql = f"""
        {_with(*src.ctes)}SELECT
            {month} AS month,
            COUNT(DISTINCT c.commit_sha) AS commits,
            NULL AS unique_authors,
            {src.lines_sum} AS total_lines_changed
        FROM commits c
        {src.lines_join}
        {where}
        GROUP BY {month}
        ORDER BY month
        """

    # unique_authors is NULL in the SQL and filled per person below.
    rows = storage.query(sql, params)

    months: dict = {}
    for r in _monthly_people(storage, config, where, params, src, scored):
        if r["author_email"] is None:
            continue
        cell = months.setdefault(r["month"], {}).setdefault(
            people.key(r["author_email"]), [0, 0.0])
        cell[0] += r["commits"]
        cell[1] += r["value"] or 0

    threshold = _min_active_commits(config)
    for row in rows:
        persons = months.get(row["month"], {})
        row["unique_authors"] = len(persons)
        if threshold > 1:
            active = [v for v in persons.values() if v[0] >= threshold]
            row["active_developers"] = len(active)
            per_dev = round(sum(v[1] for v in active) / len(active), 2) if active else None
            row["productivity_per_active_developer" if scored
                else "lines_per_active_developer"] = per_dev
    return rows


def period_comparison(storage: Storage, config: Config,
                      baseline_start: str, baseline_end: str,
                      comparison_start: str, comparison_end: str,
                      people: Optional[identity.People] = None) -> list[dict]:
    """Before/after metrics with percentage change (``unique_authors`` counts
    people, not emails)."""
    config, people = _scoped(storage, config, people)
    scored = _has_scores(storage)
    src = _source(storage, scored)

    # Build repo and exclusion filters once.
    repo_clause, repo_params = _repo_filter(config)
    excl_clause, excl_params = _excluded_devs_filter(config)

    extra = list(src.excl)
    if repo_clause:
        extra.append(repo_clause)
    if excl_clause:
        extra.append(excl_clause)

    extra_sql = (" AND " + " AND ".join(extra)) if extra else ""
    ts = dialect.timestamp_param
    date_col = _date_col(config)
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
        {_cte_prefix(src.ctes)}baseline AS (
            SELECT
                COUNT(DISTINCT c.commit_sha) AS commits,
                NULL AS unique_authors,
                ROUND(COALESCE(SUM(s.total_productivity), 0), 2) AS total_productivity,
                ROUND(COALESCE(AVG(s.weighted_score), 0), 3) AS avg_weighted_score,
                COUNT(DISTINCT CAST({date_col} AS DATE)) AS active_days
            FROM commits c
            LEFT JOIN {src.scores} s ON c.commit_sha = s.commit_sha
            WHERE {date_col} >= {ts('$b_start')} AND {date_col} < {ts('$b_end')}
                  {extra_sql}
        ),
        comparison AS (
            SELECT
                COUNT(DISTINCT c.commit_sha) AS commits,
                NULL AS unique_authors,
                ROUND(COALESCE(SUM(s.total_productivity), 0), 2) AS total_productivity,
                ROUND(COALESCE(AVG(s.weighted_score), 0), 3) AS avg_weighted_score,
                COUNT(DISTINCT CAST({date_col} AS DATE)) AS active_days
            FROM commits c
            LEFT JOIN {src.scores} s ON c.commit_sha = s.commit_sha
            WHERE {date_col} >= {ts('$c_start')} AND {date_col} < {ts('$c_end')}
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
        {_cte_prefix(src.ctes)}baseline AS (
            SELECT
                COUNT(DISTINCT c.commit_sha) AS commits,
                NULL AS unique_authors,
                {src.lines_sum} AS total_lines_changed,
                COUNT(DISTINCT CAST({date_col} AS DATE)) AS active_days
            FROM commits c
            {src.lines_join}
            WHERE {date_col} >= {ts('$b_start')} AND {date_col} < {ts('$b_end')}
                  {extra_sql}
        ),
        comparison AS (
            SELECT
                COUNT(DISTINCT c.commit_sha) AS commits,
                NULL AS unique_authors,
                {src.lines_sum} AS total_lines_changed,
                COUNT(DISTINCT CAST({date_col} AS DATE)) AS active_days
            FROM commits c
            {src.lines_join}
            WHERE {date_col} >= {ts('$c_start')} AND {date_col} < {ts('$c_end')}
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

    # unique_authors is NULL in the SQL and filled per person below.
    rows = storage.query(sql, params)
    # UNION ALL has no ORDER BY: key by label, then put baseline first.
    rows = sorted(rows, key=lambda r: r["period"] != "baseline")

    if len(rows) == 2:
        ranges = {"baseline": (baseline_start, baseline_end),
                  "comparison": (comparison_start, comparison_end)}
        for row in rows:
            start, end = ranges[row["period"]]
            emails = storage.query(f"""
                SELECT DISTINCT c.author_email AS author_email FROM commits c
                WHERE {date_col} >= {ts('$start')} AND {date_col} < {ts('$end')}
                      {extra_sql}""",
                {**repo_params, **excl_params, "start": start, "end": end})
            row["unique_authors"] = len({people.key(e["author_email"])
                                         for e in emails if e["author_email"] is not None})

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
                        since: str, until: str, limit: int = 20,
                        people: Optional[identity.People] = None) -> list[dict]:
    """Per person: commits, total productivity, avg score, top category.

    One row per person (see ``lib/identity.py``), named after the identity
    with the most commits. The groups are summed in Python: the query returns
    one row per stored identity, a few thousand at most.
    """
    config, people = _scoped(storage, config, people)
    scored = _has_scores(storage)
    src = _source(storage, scored)
    where, params = _build_where(config, since, until, src.excl)

    if scored:
        sql = f"""
        {_with(*src.ctes)}SELECT
            c.author_name AS author_name,
            c.author_email AS author_email,
            COUNT(DISTINCT c.commit_sha) AS commits,
            COALESCE(SUM(s.total_productivity), 0) AS total_productivity,
            COALESCE(SUM(s.weighted_score), 0) AS score_sum,
            COUNT(s.weighted_score) AS score_count
        FROM commits c
        LEFT JOIN {src.scores} s ON c.commit_sha = s.commit_sha
        {where}
        GROUP BY c.author_name, c.author_email
        """
        categories = storage.query(f"""
        {_with(*src.ctes)}SELECT
            c.author_email AS author_email,
            s.overall_category AS category,
            COUNT(*) AS cat_count
        FROM commits c
        JOIN {src.scores} s ON c.commit_sha = s.commit_sha
        {where}
        GROUP BY c.author_email, s.overall_category
        """, params)
    else:
        sql = f"""
        {_with(*src.ctes)}SELECT
            c.author_name AS author_name,
            c.author_email AS author_email,
            COUNT(DISTINCT c.commit_sha) AS commits,
            {src.lines_sum} AS total_lines_changed
        FROM commits c
        {src.lines_join}
        {where}
        GROUP BY c.author_name, c.author_email
        """
        categories = []

    # person key -> accumulated row; the identity with most commits names it.
    merged: dict = {}
    for r in storage.query(sql, params):
        if r["author_email"] is None:
            continue
        acc = merged.setdefault(people.key(r["author_email"]), {
            "best": (-1, "", ""), "commits": 0, "total": 0,
            "score_sum": 0.0, "score_count": 0, "cats": {}})
        acc["best"] = max(acc["best"], (r["commits"], r["author_name"] or "",
                                        r["author_email"]),
                          key=lambda b: (b[0], b[2]))
        acc["commits"] += r["commits"]
        acc["total"] += (r["total_productivity"] if scored
                         else r["total_lines_changed"]) or 0
        if scored:
            acc["score_sum"] += r["score_sum"] or 0
            acc["score_count"] += r["score_count"]
    for r in categories:
        acc = merged.get(people.key(r["author_email"]))
        if acc is not None:
            cats = acc["cats"]
            cats[r["category"]] = cats.get(r["category"], 0) + r["cat_count"]

    out = []
    for acc in merged.values():
        _count, name, email = acc["best"]
        row = {"author_name": name, "author_email": email, "commits": acc["commits"]}
        if scored:
            row["total_productivity"] = round(acc["total"], 2)
            row["avg_weighted_score"] = (
                round(acc["score_sum"] / acc["score_count"], 3) if acc["score_count"] else 0)
            row["top_category"] = (
                min(acc["cats"], key=lambda c: (-acc["cats"][c], str(c)))
                if acc["cats"] else "-")
        else:
            row["total_lines_changed"] = round(acc["total"], 1)
        out.append(row)
    sort_key = "total_productivity" if scored else "total_lines_changed"
    out.sort(key=lambda r: (-r[sort_key], -r["commits"], r["author_email"]))
    return out[:limit]


def merge_velocity(storage: Storage, config: Config,
                   since: str, until: str,
                   people: Optional[identity.People] = None) -> list[dict]:
    """Monthly: total MRs, merged count, merge rate %, avg time to merge (days),
    velocity at 7d and 30d windows."""
    config, people = _scoped(storage, config, people)
    # Use merge_requests table; filter by created_at.
    repo_clause, repo_params = _repo_filter(config)
    excl_clause, excl_params = _excluded_devs_filter(config)

    # Adapt filters for MR table aliases (m instead of c).
    # MR project_name stores short names (e.g., "admin"), not full paths
    # (e.g., "org/admin"). Rebuild repo params with short names.
    if repo_clause:
        mr_repo_params = {k: v.split("/")[-1] if "/" in v else v
                          for k, v in repo_params.items()}
        mr_repo_clause = repo_clause.replace("c.project_path", "m.project_name")
        repo_params = mr_repo_params  # override for this query
    else:
        mr_repo_clause = ""
    mr_excl_clause = excl_clause.replace("c.author_email", "m.author_email") if excl_clause else ""

    clauses = [f"m.created_at >= {dialect.timestamp_param('$since')}",
               f"m.created_at < {dialect.timestamp_param('$until')}"]
    if mr_repo_clause:
        clauses.append(mr_repo_clause)
    if mr_excl_clause:
        clauses.append(mr_excl_clause)

    where_sql = "WHERE " + " AND ".join(clauses)

    params = {"since": since, "until": until}
    params.update(repo_params)
    params.update(excl_params)

    month = dialect.month_trunc(storage, "m.created_at")
    days = dialect.days_between(storage, "m.created_at", "m.merged_at")
    merged = dialect.count_where("m.state = 'merged'")
    merged_in = "m.state = 'merged' AND m.merged_at IS NOT NULL AND "
    within_7d = dialect.count_where(f"{merged_in}{days} <= 7")
    within_30d = dialect.count_where(f"{merged_in}{days} <= 30")
    sql = f"""
    SELECT
        {month} AS month,
        COUNT(*) AS total_mrs,
        {merged} AS merged_count,
        ROUND(
            {merged} * 100.0
            / NULLIF(COUNT(*), 0), 1
        ) AS merge_rate_pct,
        ROUND(
            AVG(
                CASE WHEN m.merged_at IS NOT NULL
                     THEN {days}
                END
            ), 2
        ) AS avg_days_to_merge,
        {within_7d} AS merged_within_7d,
        {within_30d} AS merged_within_30d
    FROM {LATEST_MRS} m
    {where_sql}
    GROUP BY {month}
    ORDER BY month
    """

    return storage.query(sql, params)


def category_distribution(storage: Storage, config: Config,
                          since: str, until: str,
                          people: Optional[identity.People] = None) -> list[dict]:
    """Category, count, % of total, avg score."""
    if not _has_scores(storage):
        return []
    config, people = _scoped(storage, config, people)

    src = _source(storage, True)
    where, params = _build_where(config, since, until, src.excl)

    sql = f"""
    {_cte_prefix(src.ctes)}cats AS (
        SELECT
            s.overall_category AS category,
            COUNT(*) AS commit_count,
            ROUND(AVG(s.weighted_score), 3) AS avg_score
        FROM commits c
        JOIN {src.scores} s ON c.commit_sha = s.commit_sha
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
                    since: str, until: str,
                    people: Optional[identity.People] = None) -> list[dict]:
    """Monthly: total additions from all commits, additions from merged commits,
    efficiency ratio."""
    config, people = _scoped(storage, config, people)
    repo_clause, repo_params = _repo_filter(config)
    excl_clause, excl_params = _excluded_devs_filter(config)

    # Duplicate commits are excluded; additions are not moved-code discounted
    # (this is a ratio of raw additions, not a productivity figure).
    date_col = _date_col(config)
    clauses = [f"{date_col} >= {dialect.timestamp_param('$since')}",
               f"{date_col} < {dialect.timestamp_param('$until')}"]
    if dw.last_run(storage) is not None:
        clauses.append(_NOT_DUPLICATE)
    if repo_clause:
        clauses.append(repo_clause)
    if excl_clause:
        clauses.append(excl_clause)
    where_sql = "WHERE " + " AND ".join(clauses)

    params = {"since": since, "until": until}
    params.update(repo_params)
    params.update(excl_params)

    month = dialect.month_trunc(storage, date_col)
    sql = f"""
    WITH commit_merged AS (
        SELECT DISTINCT mc.commit_sha
        FROM mr_commits mc
        JOIN {LATEST_MRS} mr
            ON mc.mr_iid = mr.mr_iid
               AND (mc.project_id = mr.project_id
                    OR mc.project_id LIKE '%' || mr.project_name)
        WHERE mr.state = 'merged'
    )
    SELECT
        {month} AS month,
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
    GROUP BY {month}
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


# Queries that read score tables (and so need the single-model guard).
SCORE_QUERIES = {
    "monthly_trends", "period_comparison", "author_productivity",
    "category_distribution",
}


def _period_ranges(args) -> list[tuple[str, str]]:
    """Baseline and comparison windows, when all four dates were given."""
    if not (args.baseline_start and args.baseline_end
            and args.comparison_start and args.comparison_end):
        return []  # the per-query argument check reports the missing flags
    return [
        (args.baseline_start, args.baseline_end),
        (args.comparison_start, args.comparison_end),
    ]


def check_dedupe(storage: Storage, require: bool = False) -> list[str]:
    """Warnings about the state of duplicate-work detection.

    Raises ``DedupeRequiredError`` instead when *require* is set and it has
    never run. Never-run is not an error by default: analysis proceeds without
    exclusions and says so.
    """
    run = dw.last_run(storage)
    if run is None:
        msg = ("duplicate-work detection has never run: duplicates (merge, squash, "
               "promotion and copied commits, moved code) are still counted. Run "
               "/coding-productivity:dedupe, or scripts/dedupe.py.")
        if require:
            raise DedupeRequiredError(msg)
        return [f"WARNING: {msg}"]
    warnings = []
    if run["rules_version"] != dw.DEDUPE_RULES_VERSION:
        warnings.append(
            f"WARNING: the last dedupe run used rules v{run['rules_version']}, this "
            f"plugin has v{dw.DEDUPE_RULES_VERSION}. Run dedupe again."
        )
    # dedupe records distinct shas, so compare the same thing, not row count.
    now = storage.query("SELECT COUNT(DISTINCT commit_sha) AS n FROM commits")[0]["n"]
    if run["commits"] != now:
        warnings.append(
            f"WARNING: the commits table has {now:,} commits, {run['commits']:,} when "
            "dedupe last ran. Run dedupe again so new commits are checked."
        )
    return warnings


class DedupeRequiredError(RuntimeError):
    """``--require-dedupe`` was passed but dedupe has never run."""


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
    parser.add_argument(
        "--allow-legacy-null", action="store_true",
        help="Allow scores with no recorded model (legacy rows, written before "
             "provenance existed) next to scores from a single model. More than "
             "one recorded model is always refused.",
    )

    parser.add_argument(
        "--require-dedupe", action="store_true",
        help="Fail (exit 2) if duplicate-work detection has never run, instead "
             "of warning and analyzing without exclusions.",
    )

    args = parser.parse_args()

    config = Config(args.config)
    storage = get_storage(config)

    def note(msg: str) -> None:
        # Notes must not corrupt machine-readable output.
        print(msg, file=sys.stdout if args.output_format == "table" else sys.stderr)

    try:
        # Who counts as one developer: resolved once, shared by every query.
        people = load_people(storage, config)
        scope = _Scope(config, people.non_person_emails())

        # Determine queries to run.
        if args.query == "all":
            query_names = [q for q in QUERIES if q != "period_comparison"]
        else:
            query_names = [args.query]

        # One series = one model: refuse mixed-model score ranges up front.
        if any(q in SCORE_QUERIES for q in query_names) and _has_scores(storage):
            if "period_comparison" in query_names:
                ranges = _period_ranges(args)
            else:
                ranges = [(args.since, args.until)] if args.since and args.until else []
            if ranges:
                try:
                    check_single_scorer_model(
                        storage, scope, ranges,
                        allow_legacy_null=args.allow_legacy_null,
                    )
                except MixedScorerModelError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    sys.exit(2)

        # Duplicate work: warn (or refuse) when it never ran, else say what it removed.
        try:
            for warning in check_dedupe(storage, require=args.require_dedupe):
                print(warning, file=sys.stderr)
        except DedupeRequiredError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(2)
        if "period_comparison" in query_names:
            labelled = [("baseline", args.baseline_start, args.baseline_end),
                        ("comparison", args.comparison_start, args.comparison_end)]
        else:
            labelled = [("", args.since, args.until)]
        for label, start, end in labelled:
            if start and end:
                line = dedupe_summary(storage, scope, start, end)
                if line:
                    note(f"{label + ' ' if label else ''}{line}")
        for line in identity_notes(storage, config, people):
            note(line)

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
                    people=people,
                )
            else:
                if not args.since or not args.until:
                    print("Error: --since and --until are required.",
                          file=sys.stderr)
                    sys.exit(1)

                if qname == "author_productivity":
                    rows = author_productivity(
                        storage, config, args.since, args.until,
                        limit=args.limit, people=people,
                    )
                else:
                    rows = QUERIES[qname](storage, config, args.since, args.until,
                                          people=people)

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
