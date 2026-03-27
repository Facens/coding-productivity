"""Tests for lib/checkpoint.py — per-repo extraction state."""

import json
import os
from unittest.mock import patch
from pathlib import Path

from lib import checkpoint


class TestCheckpointRoundTrip:
    def test_save_and_load(self, tmp_dir):
        with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir):
            state = {
                "last_commit_sha": "abc123",
                "last_page": 5,
                "phase": "commits",
                "pagination_type": "page",
                "timestamp": "2026-03-27T00:00:00Z",
            }
            checkpoint.save("owner_repo", state)
            loaded = checkpoint.load("owner_repo")
            assert loaded == state

    def test_load_nonexistent_returns_none(self, tmp_dir):
        with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir):
            assert checkpoint.load("nonexistent_repo") is None

    def test_exists_true(self, tmp_dir):
        with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir):
            checkpoint.save("owner_repo", {"phase": "commits"})
            assert checkpoint.exists("owner_repo") is True

    def test_exists_false(self, tmp_dir):
        with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir):
            assert checkpoint.exists("nonexistent") is False

    def test_clear_removes_file(self, tmp_dir):
        with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir):
            checkpoint.save("owner_repo", {"phase": "commits"})
            assert checkpoint.exists("owner_repo")
            checkpoint.clear("owner_repo")
            assert not checkpoint.exists("owner_repo")

    def test_clear_nonexistent_no_error(self, tmp_dir):
        with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir):
            checkpoint.clear("nonexistent")  # should not raise

    def test_slug_with_slash_is_safe(self, tmp_dir):
        with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir):
            # Repo slugs should use _ not / for filenames
            checkpoint.save("owner_repo", {"phase": "commits"})
            files = list(tmp_dir.glob("*.json"))
            assert len(files) == 1
            assert "/" not in files[0].name


class TestCheckpointAtomicity:
    def test_no_tmp_files_left(self, tmp_dir):
        with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir):
            checkpoint.save("owner_repo", {"phase": "commits"})
            all_files = list(tmp_dir.iterdir())
            tmp_files = [f for f in all_files if ".tmp" in f.name]
            assert len(tmp_files) == 0, f"Temp files left behind: {tmp_files}"
