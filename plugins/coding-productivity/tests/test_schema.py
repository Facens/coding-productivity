"""Tests for lib/schema.py — table definitions and DDL generation."""

from lib.schema import SCHEMAS, get_create_sql, get_columns


EXPECTED_TABLES = ["commits", "diffs", "merge_requests", "mr_commits", "commit_scores", "file_scores"]


def test_all_tables_defined():
    for table in EXPECTED_TABLES:
        assert table in SCHEMAS, f"Missing table: {table}"


def test_no_author_mapping_table():
    """author_mapping is NOT a storage table — identity uses JSON file + config."""
    assert "author_mapping" not in SCHEMAS


def test_get_columns_returns_list():
    for table in EXPECTED_TABLES:
        cols = get_columns(table)
        assert isinstance(cols, list)
        assert len(cols) > 0


def test_commits_has_required_columns():
    cols = get_columns("commits")
    required = ["commit_sha", "author_name", "author_email", "authored_date", "additions", "deletions"]
    for col in required:
        assert col in cols, f"commits missing column: {col}"


def test_get_create_sql_is_valid_ddl():
    for table in EXPECTED_TABLES:
        ddl = get_create_sql(table)
        assert ddl.startswith("CREATE TABLE IF NOT EXISTS")
        assert table in ddl


def test_file_scores_has_score_column():
    cols = get_columns("file_scores")
    assert "score" in cols
    assert "category" in cols
    assert "reasoning" in cols
