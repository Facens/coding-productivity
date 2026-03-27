"""Tests for lib/storage.py — DuckDB storage backend."""

import pytest
from lib.storage import DuckDBStorage


class TestDuckDBStorageCreate:
    def test_create_tables_succeeds(self, duckdb_storage):
        # Tables already created by fixture; verify they exist
        rows = duckdb_storage.query("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'")
        table_names = {r["table_name"] for r in rows}
        assert "commits" in table_names
        assert "diffs" in table_names
        assert "commit_scores" in table_names

    def test_create_tables_idempotent(self, duckdb_storage):
        # Calling again should not raise
        duckdb_storage.create_tables()


class TestDuckDBStorageInsert:
    def test_insert_and_count(self, duckdb_storage, sample_commits):
        duckdb_storage.insert_batch("commits", sample_commits)
        assert duckdb_storage.count("commits") == 2

    def test_insert_empty_batch(self, duckdb_storage):
        duckdb_storage.insert_batch("commits", [])
        assert duckdb_storage.count("commits") == 0

    def test_insert_preserves_data(self, duckdb_storage, sample_commits):
        duckdb_storage.insert_batch("commits", sample_commits)
        rows = duckdb_storage.query("SELECT commit_sha, title, additions FROM commits ORDER BY additions DESC")
        assert rows[0]["commit_sha"] == "abc123def456"
        assert rows[0]["additions"] == 50
        assert rows[1]["additions"] == 5


class TestDuckDBStorageQuery:
    def test_query_no_params(self, duckdb_storage, sample_commits):
        duckdb_storage.insert_batch("commits", sample_commits)
        rows = duckdb_storage.query("SELECT COUNT(*) AS cnt FROM commits")
        assert rows[0]["cnt"] == 2

    def test_query_with_params(self, duckdb_storage, sample_commits):
        duckdb_storage.insert_batch("commits", sample_commits)
        rows = duckdb_storage.query(
            "SELECT commit_sha FROM commits WHERE additions > ?",
            {"min_additions": 10},
        )
        assert len(rows) == 1
        assert rows[0]["commit_sha"] == "abc123def456"

    def test_get_existing_shas(self, duckdb_storage, sample_commits):
        duckdb_storage.insert_batch("commits", sample_commits)
        shas = duckdb_storage.get_existing_shas("commits")
        assert shas == {"abc123def456", "def456abc789"}

    def test_get_existing_shas_empty_table(self, duckdb_storage):
        shas = duckdb_storage.get_existing_shas("commits")
        assert shas == set()


class TestDuckDBStorageReadOnly:
    def test_readonly_blocks_insert(self, tmp_dir, sample_commits):
        # First create with data
        db_path = str(tmp_dir / "ro_test.duckdb")
        rw = DuckDBStorage(db_path)
        rw.create_tables()
        rw.insert_batch("commits", sample_commits)
        rw.close()

        # Open read-only
        ro = DuckDBStorage(db_path, readonly=True)
        with pytest.raises(RuntimeError, match="read-only"):
            ro.insert_batch("commits", sample_commits[:1])
        ro.close()

    def test_readonly_blocks_create_tables(self, tmp_dir):
        db_path = str(tmp_dir / "ro_test2.duckdb")
        rw = DuckDBStorage(db_path)
        rw.create_tables()
        rw.close()

        ro = DuckDBStorage(db_path, readonly=True)
        with pytest.raises(RuntimeError, match="read-only"):
            ro.create_tables()
        ro.close()

    def test_readonly_allows_query(self, tmp_dir, sample_commits):
        db_path = str(tmp_dir / "ro_test3.duckdb")
        rw = DuckDBStorage(db_path)
        rw.create_tables()
        rw.insert_batch("commits", sample_commits)
        rw.close()

        ro = DuckDBStorage(db_path, readonly=True)
        assert ro.count("commits") == 2
        ro.close()


class TestDuckDBStorageWithScores:
    def test_insert_and_query_scores(self, duckdb_storage, sample_commits, sample_scores):
        duckdb_storage.insert_batch("commits", sample_commits)
        duckdb_storage.insert_batch("commit_scores", sample_scores)

        rows = duckdb_storage.query(
            "SELECT c.commit_sha, s.weighted_score, s.overall_category "
            "FROM commits c JOIN commit_scores s ON c.commit_sha = s.commit_sha "
            "ORDER BY s.weighted_score DESC"
        )
        assert len(rows) == 2
        assert rows[0]["overall_category"] == "feature"
        assert rows[0]["weighted_score"] == 0.30
