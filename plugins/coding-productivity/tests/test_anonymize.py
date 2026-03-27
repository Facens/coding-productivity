"""Tests for lib/anonymize.py — HMAC pseudonymization and reverse mapping."""

import json
from pathlib import Path

from lib.anonymize import (
    generate_salt,
    load_key,
    pseudonymize,
    hash_record,
    build_or_update_mapping,
    load_mapping,
    resolve_merge,
)


class TestSaltAndKey:
    def test_generate_salt_length(self):
        salt = generate_salt()
        assert len(salt) == 64  # 32 bytes = 64 hex chars

    def test_generate_salt_is_hex(self):
        salt = generate_salt()
        int(salt, 16)  # should not raise

    def test_generate_salt_is_random(self):
        s1 = generate_salt()
        s2 = generate_salt()
        assert s1 != s2

    def test_load_key_returns_bytes(self):
        salt = generate_salt()
        key = load_key(salt)
        assert isinstance(key, bytes)
        assert len(key) == 32


class TestPseudonymize:
    def test_deterministic(self):
        key = load_key(generate_salt())
        h1 = pseudonymize("test@example.com", key)
        h2 = pseudonymize("test@example.com", key)
        assert h1 == h2

    def test_different_input_different_hash(self):
        key = load_key(generate_salt())
        h1 = pseudonymize("alice@example.com", key)
        h2 = pseudonymize("bob@example.com", key)
        assert h1 != h2

    def test_hash_length_is_12(self):
        key = load_key(generate_salt())
        h = pseudonymize("test@example.com", key)
        assert len(h) == 12

    def test_case_insensitive(self):
        key = load_key(generate_salt())
        h1 = pseudonymize("Test@Example.COM", key)
        h2 = pseudonymize("test@example.com", key)
        assert h1 == h2

    def test_different_key_different_hash(self):
        k1 = load_key(generate_salt())
        k2 = load_key(generate_salt())
        h1 = pseudonymize("test@example.com", k1)
        h2 = pseudonymize("test@example.com", k2)
        assert h1 != h2


class TestHashRecord:
    def test_hashes_specified_columns(self):
        key = load_key(generate_salt())
        record = {"author_name": "Alice", "author_email": "alice@co.com", "title": "feat"}
        result = hash_record(record, ["author_name", "author_email"], key)
        assert len(result["author_name"]) == 12
        assert len(result["author_email"]) == 12
        assert result["title"] == "feat"  # unchanged

    def test_skips_missing_columns(self):
        key = load_key(generate_salt())
        record = {"author_name": "Alice"}
        result = hash_record(record, ["author_name", "author_email"], key)
        assert len(result["author_name"]) == 12
        assert "author_email" not in result or result.get("author_email") is None

    def test_with_merges(self):
        key = load_key(generate_salt())
        merges = {"alice.old@co.com": "alice@co.com"}
        record = {"author_email": "alice.old@co.com"}
        result = hash_record(record, ["author_email"], key, merges=merges)
        # Should produce the canonical hash
        expected = pseudonymize("alice@co.com", key)
        assert result["author_email"] == expected


class TestResolveMerge:
    def test_resolves_alias(self):
        merges = {"alias@old.com": "canonical@new.com"}
        assert resolve_merge("alias@old.com", merges) == "canonical@new.com"

    def test_passthrough_unknown(self):
        merges = {"alias@old.com": "canonical@new.com"}
        assert resolve_merge("other@test.com", merges) == "other@test.com"

    def test_case_insensitive(self):
        merges = {"alias@old.com": "canonical@new.com"}
        assert resolve_merge("ALIAS@OLD.COM", merges) == "canonical@new.com"


class TestReverseMapping:
    def test_build_and_load(self, tmp_dir):
        mapping_path = tmp_dir / "mapping.json"
        build_or_update_mapping("Alice", "alice@co.com", "hash_abc123", mapping_path)
        mapping = load_mapping(mapping_path)
        assert "hash_abc123" in mapping
        assert mapping["hash_abc123"]["name"] == "Alice"
        assert mapping["hash_abc123"]["email"] == "alice@co.com"

    def test_incremental_update(self, tmp_dir):
        mapping_path = tmp_dir / "mapping.json"
        build_or_update_mapping("Alice", "alice@co.com", "hash_aaa", mapping_path)
        build_or_update_mapping("Bob", "bob@co.com", "hash_bbb", mapping_path)
        mapping = load_mapping(mapping_path)
        assert len(mapping) == 2
        assert "hash_aaa" in mapping
        assert "hash_bbb" in mapping

    def test_load_nonexistent_returns_empty(self, tmp_dir):
        mapping = load_mapping(tmp_dir / "nonexistent.json")
        assert mapping == {}

    def test_file_permissions(self, tmp_dir):
        import os
        import stat
        mapping_path = tmp_dir / "mapping.json"
        build_or_update_mapping("Alice", "alice@co.com", "hash_abc", mapping_path)
        mode = os.stat(mapping_path).st_mode
        # Should be owner read/write only (0o600)
        assert not (mode & stat.S_IRGRP), "Group should not have read access"
        assert not (mode & stat.S_IROTH), "Others should not have read access"
