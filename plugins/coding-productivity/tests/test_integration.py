"""Integration tests — full pipeline scenarios using real DuckDB."""

import json
from pathlib import Path

from lib.storage import DuckDBStorage
from lib.anonymize import generate_salt, load_key, pseudonymize, hash_record, build_or_update_mapping, load_mapping
from lib.bots import is_bot
from lib.dedup import find_duplicates


class TestExtractionPipeline:
    """Simulates the extract → anonymize → store → query flow."""

    def test_full_extract_anonymize_store_flow(self, duckdb_storage, tmp_dir):
        """Commits are anonymized, stored, and queryable."""
        salt = generate_salt()
        key = load_key(salt)
        mapping_path = tmp_dir / "developer_mapping.json"

        # Simulate raw API data (pre-anonymization)
        raw_commits = [
            {
                "commit_sha": "sha_001",
                "project_id": "1",
                "project_name": "myrepo",
                "project_path": "org/myrepo",
                "author_name": "Alice Johnson",
                "author_email": "alice@company.com",
                "committer_name": "Alice Johnson",
                "committer_email": "alice@company.com",
                "authored_date": "2026-01-15T10:00:00Z",
                "committed_date": "2026-01-15T10:00:00Z",
                "created_at": "2026-01-15T10:00:00Z",
                "branch_name": "main",
                "title": "feat: add auth",
                "message": "feat: add auth flow",
                "additions": 100,
                "deletions": 20,
                "total_changes": 120,
                "parent_ids": "",
                "web_url": "",
                "extracted_at": "2026-03-27T00:00:00Z",
            },
            {
                "commit_sha": "sha_002",
                "project_id": "1",
                "project_name": "myrepo",
                "project_path": "org/myrepo",
                "author_name": "dependabot[bot]",
                "author_email": "49699333+dependabot[bot]@users.noreply.github.com",
                "committer_name": "dependabot[bot]",
                "committer_email": "49699333+dependabot[bot]@users.noreply.github.com",
                "authored_date": "2026-01-16T10:00:00Z",
                "committed_date": "2026-01-16T10:00:00Z",
                "created_at": "2026-01-16T10:00:00Z",
                "branch_name": "main",
                "title": "chore(deps): bump lodash",
                "message": "chore(deps): bump lodash from 4.17.20 to 4.17.21",
                "additions": 5,
                "deletions": 5,
                "total_changes": 10,
                "parent_ids": "",
                "web_url": "",
                "extracted_at": "2026-03-27T00:00:00Z",
            },
        ]

        identity_cols = ["author_name", "author_email", "committer_name", "committer_email"]
        processed = []

        for commit in raw_commits:
            # Skip bots
            if is_bot(commit["author_name"], commit["author_email"]):
                continue

            # Build reverse mapping BEFORE hashing
            original_name = commit["author_name"]
            original_email = commit["author_email"]
            hashed_email = pseudonymize(original_email, key)
            build_or_update_mapping(original_name, original_email, hashed_email, mapping_path)

            # Anonymize
            commit = hash_record(commit, identity_cols, key)
            processed.append(commit)

        # Store
        duckdb_storage.insert_batch("commits", processed)

        # Verify: only 1 commit (bot filtered)
        assert duckdb_storage.count("commits") == 1

        # Verify: stored data is hashed
        rows = duckdb_storage.query("SELECT author_name, author_email, title FROM commits")
        assert len(rows) == 1
        assert len(rows[0]["author_name"]) == 12  # hashed
        assert rows[0]["author_name"] != "Alice Johnson"
        assert rows[0]["title"] == "feat: add auth"  # title NOT hashed

        # Verify: mapping file has the real identity
        mapping = load_mapping(mapping_path)
        assert len(mapping) == 1
        entry = list(mapping.values())[0]
        assert entry["name"] == "Alice Johnson"
        assert entry["email"] == "alice@company.com"

    def test_dedup_prevents_duplicate_inserts(self, duckdb_storage, sample_commits):
        """Extractor pattern: load existing SHAs, skip known commits."""
        duckdb_storage.insert_batch("commits", sample_commits)
        existing_shas = duckdb_storage.get_existing_shas("commits")

        # Simulate second extraction with overlap
        new_commits = sample_commits + [
            {
                "commit_sha": "brand_new_sha",
                "project_id": "1",
                "project_name": "test-repo",
                "project_path": "org/test-repo",
                "author_name": "hash_eee",
                "author_email": "hash_fff",
                "committer_name": "hash_eee",
                "committer_email": "hash_fff",
                "authored_date": "2026-03-01T10:00:00Z",
                "committed_date": "2026-03-01T10:00:00Z",
                "created_at": "2026-03-01T10:00:00Z",
                "branch_name": "main",
                "title": "new commit",
                "message": "new commit",
                "additions": 10,
                "deletions": 0,
                "total_changes": 10,
                "parent_ids": "",
                "web_url": "",
                "extracted_at": "2026-03-27T00:00:00Z",
            }
        ]

        # Filter duplicates (as the extractor does)
        to_insert = [c for c in new_commits if c["commit_sha"] not in existing_shas]
        duckdb_storage.insert_batch("commits", to_insert)

        assert duckdb_storage.count("commits") == 3  # 2 original + 1 new

    def test_identity_merge_produces_canonical_hash(self, tmp_dir):
        """When IDENTITY_MERGES is configured, aliases produce the canonical hash."""
        salt = generate_salt()
        key = load_key(salt)
        merges = {"alice.old@company.com": "alice@company.com"}

        record = {
            "author_name": "Alice (old)",
            "author_email": "alice.old@company.com",
        }

        result = hash_record(record, ["author_name", "author_email"], key, merges=merges)

        # The email should hash as the CANONICAL, not the alias
        expected_hash = pseudonymize("alice@company.com", key)
        assert result["author_email"] == expected_hash


class TestAnalysisQueries:
    """Test that analysis queries work against populated DuckDB."""

    def test_monthly_trends_query(self, duckdb_storage, sample_commits, sample_scores):
        duckdb_storage.insert_batch("commits", sample_commits)
        duckdb_storage.insert_batch("commit_scores", sample_scores)

        rows = duckdb_storage.query(
            "SELECT date_trunc('month', authored_date::TIMESTAMP) AS month, "
            "COUNT(*) AS commit_count, "
            "COUNT(DISTINCT author_name) AS unique_authors "
            "FROM commits "
            "GROUP BY 1 ORDER BY 1"
        )
        assert len(rows) == 2  # Jan and Feb
        assert rows[0]["commit_count"] == 1

    def test_category_distribution_query(self, duckdb_storage, sample_commits, sample_scores):
        duckdb_storage.insert_batch("commits", sample_commits)
        duckdb_storage.insert_batch("commit_scores", sample_scores)

        rows = duckdb_storage.query(
            "SELECT overall_category, COUNT(*) AS cnt "
            "FROM commit_scores "
            "GROUP BY 1 ORDER BY cnt DESC"
        )
        assert len(rows) == 2
        categories = {r["overall_category"] for r in rows}
        assert "feature" in categories
        assert "bugfix" in categories

    def test_analysis_without_scores(self, duckdb_storage, sample_commits):
        """Analysis works with raw metrics when no scores exist."""
        duckdb_storage.insert_batch("commits", sample_commits)

        rows = duckdb_storage.query(
            "SELECT date_trunc('month', authored_date::TIMESTAMP) AS month, "
            "COUNT(*) AS commit_count, "
            "SUM(additions) AS total_additions "
            "FROM commits "
            "GROUP BY 1 ORDER BY 1"
        )
        assert len(rows) == 2
        assert rows[0]["total_additions"] == 50

    def test_duckdb_week_truncation(self, duckdb_storage, sample_commits):
        """Verify date_trunc('week', ...) works (DuckDB starts weeks on Monday)."""
        duckdb_storage.insert_batch("commits", sample_commits)

        rows = duckdb_storage.query(
            "SELECT date_trunc('week', authored_date::TIMESTAMP) AS week_start, "
            "COUNT(*) AS cnt FROM commits GROUP BY 1"
        )
        assert len(rows) >= 1  # at least one week bucket

    def test_count_filter_syntax(self, duckdb_storage, sample_commits, sample_scores):
        """Verify DuckDB-compatible COUNT(*) FILTER (WHERE ...) works."""
        duckdb_storage.insert_batch("commits", sample_commits)
        duckdb_storage.insert_batch("commit_scores", sample_scores)

        rows = duckdb_storage.query(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE weighted_score > 0.28) AS high_score "
            "FROM commit_scores"
        )
        assert rows[0]["total"] == 2
        assert rows[0]["high_score"] == 1  # only the 0.30 score
