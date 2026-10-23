"""
SQL that differs between DuckDB and BigQuery, written once.

``analyze.py`` and the other readers build their SQL as strings and run it on
whichever backend ``STORAGE_BACKEND`` selects. Anything the two engines spell
differently goes through this module, so a query is written once and is
correct on both. Prefer SQL both engines share (``CAST(x AS TIMESTAMP)``,
``CASE WHEN`` instead of ``FILTER``) and add a function here only when no such
spelling exists.

Backends expose their dialect as ``Storage.dialect`` (``"duckdb"`` or
``"bigquery"``); an object without it (a test double) is read as DuckDB.
"""

from __future__ import annotations

DUCKDB = "duckdb"
BIGQUERY = "bigquery"


def of(storage) -> str:
    """Dialect name of *storage* (``"duckdb"`` when it does not say)."""
    return getattr(storage, "dialect", DUCKDB)


def timestamp_param(placeholder: str) -> str:
    """A ``$name`` string parameter as a TIMESTAMP, same SQL on both engines.

    BigQuery does not compare a TIMESTAMP column with a STRING parameter
    (``'2026-01-01'``) unless it is cast; DuckDB accepts the cast too.
    """
    return f"CAST({placeholder} AS TIMESTAMP)"


def month_trunc(storage, expr: str) -> str:
    """First day of the month of timestamp *expr*, as a naive timestamp.

    DuckDB has ``date_trunc('month', ts)``; BigQuery's ``DATE_TRUNC`` takes a
    date part as a bare keyword and ``TIMESTAMP_TRUNC`` returns a timestamp
    with a time zone, so BigQuery goes through ``DATETIME`` to return the same
    naive value DuckDB does.
    """
    if of(storage) == BIGQUERY:
        return f"DATETIME_TRUNC(DATETIME({expr}), MONTH)"
    return f"date_trunc('month', {expr})"


def days_between(storage, start: str, end: str) -> str:
    """Fractional days from timestamp *start* to timestamp *end*."""
    if of(storage) == BIGQUERY:
        return f"TIMESTAMP_DIFF({end}, {start}, MICROSECOND) / 86400000000.0"
    return f"EXTRACT(EPOCH FROM ({end} - {start})) / 86400.0"


def count_where(condition: str) -> str:
    """Rows matching *condition*: ``COUNT(*) FILTER (WHERE ...)`` is DuckDB
    only, ``COUNTIF`` is BigQuery only; the ``CASE`` form is both."""
    return f"SUM(CASE WHEN {condition} THEN 1 ELSE 0 END)"
