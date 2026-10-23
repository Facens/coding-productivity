"""lib/dialect.py: the SQL both engines share, and what differs."""

from types import SimpleNamespace as NS

import analyze
from lib import dialect

from .test_duplicate_work import commit, link, mr

DUCK, BQ = NS(dialect="duckdb"), NS(dialect="bigquery")


def test_dialect_defaults_to_duckdb_and_backends_declare_theirs(duckdb_storage):
    assert dialect.of(NS()) == "duckdb"
    assert dialect.of(duckdb_storage) == "duckdb"
    from lib.storage import BigQueryStorage
    assert BigQueryStorage.dialect == "bigquery"


def test_month_trunc_per_engine():
    assert dialect.month_trunc(DUCK, "c.d") == "date_trunc('month', c.d)"
    assert dialect.month_trunc(BQ, "c.d") == "DATETIME_TRUNC(DATETIME(c.d), MONTH)"


def test_days_between_per_engine():
    assert "EXTRACT(EPOCH FROM (b - a))" in dialect.days_between(DUCK, "a", "b")
    assert dialect.days_between(BQ, "a", "b").startswith("TIMESTAMP_DIFF(b, a, MICROSECOND)")


def test_shared_spellings():
    assert dialect.timestamp_param("$since") == "CAST($since AS TIMESTAMP)"
    assert dialect.count_where("x = 1") == "SUM(CASE WHEN x = 1 THEN 1 ELSE 0 END)"


def test_merge_velocity_values_on_duckdb(duckdb_storage):
    st = duckdb_storage
    day = "2026-01-05T00:00:00Z"
    rows = [
        dict(mr(1, state="merged"), mr_id="a", created_at=day, merged_at="2026-01-06T12:00:00Z"),
        dict(mr(2, state="merged"), mr_id="b", created_at=day, merged_at="2026-02-20T00:00:00Z"),
        dict(mr(3, state="opened"), mr_id="c", created_at=day),
    ]
    st.insert_batch("merge_requests", rows)
    cfg = NS(REPOS=[], EXCLUDED_DEVELOPERS=[])
    (row,) = analyze.merge_velocity(st, cfg, "2026-01-01", "2026-02-01")
    assert (row["total_mrs"], row["merged_count"]) == (3, 2)
    assert row["merge_rate_pct"] == 66.7
    assert (row["merged_within_7d"], row["merged_within_30d"]) == (1, 1)
    assert row["avg_days_to_merge"] == 23.75  # (1.5 + 46) / 2


def test_code_efficiency_links_numeric_project_ids(duckdb_storage):
    """Data imported with platform ids has the same project_id on both sides;
    a fresh GitHub extract has an owner/name slug in mr_commits instead."""
    st = duckdb_storage
    st.insert_batch("merge_requests", [
        dict(mr(1, project_id="42", name="repo"), mr_id="a", created_at="2026-01-05T00:00:00Z")])
    st.insert_batch("commits", [commit("c1", adds=10), commit("c2", adds=30)])
    st.insert_batch("mr_commits", [
        {"mr_id": "a", "mr_iid": "1", "project_id": "42", "commit_sha": "c1"}])
    eff = analyze.code_efficiency(st, NS(REPOS=[], EXCLUDED_DEVELOPERS=[]),
                                  "2026-01-01", "2026-02-01")[0]
    assert (eff["total_additions"], eff["merged_additions"]) == (40, 10)
