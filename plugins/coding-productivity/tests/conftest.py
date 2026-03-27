"""Shared fixtures for coding-productivity tests."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure scripts/lib is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture
def tmp_dir():
    """Provide a temporary directory that's cleaned up after the test."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def duckdb_storage(tmp_dir):
    """Provide a DuckDBStorage instance with tables created."""
    from lib.storage import DuckDBStorage

    db_path = str(tmp_dir / "test.duckdb")
    storage = DuckDBStorage(db_path)
    storage.create_tables()
    yield storage
    storage.close()


@pytest.fixture
def sample_commits():
    """Two sample commit records matching the schema."""
    return [
        {
            "commit_sha": "abc123def456",
            "project_id": "1",
            "project_name": "test-repo",
            "project_path": "org/test-repo",
            "author_name": "hash_aaa111",
            "author_email": "hash_bbb222",
            "committer_name": "hash_aaa111",
            "committer_email": "hash_bbb222",
            "authored_date": "2026-01-15T10:00:00Z",
            "committed_date": "2026-01-15T10:00:00Z",
            "created_at": "2026-01-15T10:00:00Z",
            "branch_name": "main",
            "title": "feat: add user auth",
            "message": "feat: add user authentication flow",
            "additions": 50,
            "deletions": 10,
            "total_changes": 60,
            "parent_ids": "",
            "web_url": "https://github.com/org/test-repo/commit/abc123",
            "extracted_at": "2026-03-27T00:00:00Z",
        },
        {
            "commit_sha": "def456abc789",
            "project_id": "1",
            "project_name": "test-repo",
            "project_path": "org/test-repo",
            "author_name": "hash_ccc333",
            "author_email": "hash_ddd444",
            "committer_name": "hash_ccc333",
            "committer_email": "hash_ddd444",
            "authored_date": "2026-02-15T10:00:00Z",
            "committed_date": "2026-02-15T10:00:00Z",
            "created_at": "2026-02-15T10:00:00Z",
            "branch_name": "main",
            "title": "fix: resolve login bug",
            "message": "fix: resolve login bug on mobile",
            "additions": 5,
            "deletions": 3,
            "total_changes": 8,
            "parent_ids": "abc123def456",
            "web_url": "https://github.com/org/test-repo/commit/def456",
            "extracted_at": "2026-03-27T00:00:00Z",
        },
    ]


@pytest.fixture
def sample_scores():
    """Score records matching the two sample commits."""
    return [
        {
            "commit_sha": "abc123def456",
            "total_productivity": 15.0,
            "weighted_score": 0.30,
            "overall_category": "feature",
            "scored_at": "2026-03-27T00:00:00Z",
        },
        {
            "commit_sha": "def456abc789",
            "total_productivity": 2.0,
            "weighted_score": 0.25,
            "overall_category": "bugfix",
            "scored_at": "2026-03-27T00:00:00Z",
        },
    ]
