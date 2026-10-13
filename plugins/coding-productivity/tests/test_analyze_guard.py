"""analyze.py refuses to mix scoring models inside the selected date range."""

from types import SimpleNamespace as NS

import pytest

import analyze
from lib import scoring_meta
from lib.storage import DuckDBStorage

RANGE = [("2026-01-01", "2026-04-01")]
CFG = NS(REPOS=[], EXCLUDED_DEVELOPERS=[])


def score(sha, model, table="commit_scores"):
    return {"commit_sha": sha, "total_productivity": 10.0, "weighted_score": 0.5,
            "overall_category": "feature", "scored_at": "2026-03-27T00:00:00Z",
            "scorer_model": model, "prompt_version": "pv" if model else None}


@pytest.fixture
def two_commits(duckdb_storage, sample_commits):
    duckdb_storage.insert_batch("commits", sample_commits)  # Jan 15 and Feb 15
    return duckdb_storage


def put(storage, a, b, table="commit_scores"):
    storage.insert_batch(table, [score("abc123def456", a), score("def456abc789", b)])


def test_single_model_passes(two_commits):
    put(two_commits, "claude-haiku-5-5", "claude-haiku-5-5")
    analyze.check_single_scorer_model(two_commits, CFG, RANGE)


def test_two_models_in_range_raise(two_commits):
    put(two_commits, "claude-haiku-4-5", "claude-haiku-5-5")
    with pytest.raises(analyze.MixedScorerModelError, match="more than one scoring model"):
        analyze.check_single_scorer_model(two_commits, CFG, RANGE)


def test_two_models_always_refused_even_with_allow_legacy_null(two_commits):
    put(two_commits, "claude-haiku-4-5", "claude-haiku-5-5")
    with pytest.raises(analyze.MixedScorerModelError):
        analyze.check_single_scorer_model(two_commits, CFG, RANGE, allow_legacy_null=True)


def test_null_plus_model_raises_unless_allowed(two_commits):
    put(two_commits, None, "claude-haiku-5-5")
    with pytest.raises(analyze.MixedScorerModelError, match="--allow-legacy-null"):
        analyze.check_single_scorer_model(two_commits, CFG, RANGE)
    analyze.check_single_scorer_model(two_commits, CFG, RANGE, allow_legacy_null=True)


def test_all_null_legacy_data_is_fine(two_commits):
    put(two_commits, None, None)
    analyze.check_single_scorer_model(two_commits, CFG, RANGE)


def test_only_the_selected_range_counts(two_commits):
    put(two_commits, "claude-haiku-4-5", "claude-haiku-5-5")
    # January only: the Feb commit (other model) is outside the range.
    analyze.check_single_scorer_model(two_commits, CFG, [("2026-01-01", "2026-02-01")])


def test_period_comparison_checks_both_windows(two_commits):
    put(two_commits, "claude-haiku-4-5", "claude-haiku-5-5")
    windows = [("2026-01-01", "2026-02-01"), ("2026-02-01", "2026-03-01")]
    with pytest.raises(analyze.MixedScorerModelError):
        analyze.check_single_scorer_model(two_commits, CFG, windows)


def test_guard_reads_the_active_versioned_table(two_commits):
    put(two_commits, "claude-haiku-4-5", "claude-haiku-5-5")  # mixed legacy table
    two_commits.ensure_table("commit_scores_v2")
    put(two_commits, "claude-haiku-5-5", "claude-haiku-5-5", table="commit_scores_v2")
    scoring_meta.record(two_commits, 2, "active", "claude-haiku-5-5", "pv")
    assert analyze._commit_scores_table(two_commits) == "commit_scores_v2"
    analyze.check_single_scorer_model(two_commits, CFG, RANGE)  # v2 is clean


def test_table_without_provenance_column_is_treated_as_all_null(tmp_dir):
    from lib import schema
    storage = DuckDBStorage(str(tmp_dir / "old.duckdb"))  # no create_tables(): old layout
    storage._conn.execute(schema.get_create_sql("commits"))
    storage._conn.execute(
        "CREATE TABLE commit_scores (commit_sha VARCHAR, total_productivity DOUBLE, "
        "weighted_score DOUBLE, overall_category VARCHAR, scored_at TIMESTAMP)")
    storage._conn.execute(
        "INSERT INTO commits (commit_sha, authored_date) VALUES ('a', '2026-01-15')")
    storage._conn.execute(
        "INSERT INTO commit_scores VALUES ('a', 1, 0.5, 'feature', '2026-01-16')")
    analyze.check_single_scorer_model(storage, CFG, RANGE)
    storage.close()


def test_queries_read_the_active_table(two_commits):
    two_commits.ensure_table("commit_scores_v2")
    two_commits.insert_batch("commit_scores_v2", [
        score("abc123def456", "claude-haiku-5-5"), score("def456abc789", "claude-haiku-5-5")])
    scoring_meta.record(two_commits, 2, "active", "claude-haiku-5-5", "pv")
    rows = analyze.monthly_trends(two_commits, CFG, "2026-01-01", "2026-04-01")
    assert sum(r["total_productivity"] for r in rows) == 20.0
    assert analyze._has_scores(two_commits)
