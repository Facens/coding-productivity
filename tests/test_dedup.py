"""Tests for lib/dedup.py — identity deduplication."""

from lib.dedup import find_duplicates, normalize_name, GENERIC_NAMES


class TestNormalizeName:
    def test_lowercase(self):
        assert normalize_name("John Doe") == "johndoe"

    def test_strips_dots(self):
        assert normalize_name("jane.smith") == "janesmith"

    def test_strips_hyphens(self):
        assert normalize_name("jane-smith") == "janesmith"

    def test_strips_underscores(self):
        assert normalize_name("jane_smith") == "janesmith"

    def test_strips_whitespace(self):
        assert normalize_name("  John   Doe  ") == "johndoe"

    def test_combined(self):
        assert normalize_name("J. Doe-Smith_Jr") == "jdoesmithjr"


class TestFindDuplicates:
    def test_exact_name_different_email(self):
        devs = [
            {"name": "John Doe", "email": "john@work.com", "commit_count": 50},
            {"name": "John Doe", "email": "john@personal.com", "commit_count": 10},
        ]
        dupes = find_duplicates(devs)
        assert len(dupes) >= 1
        emails = {d[0]["email"] for d in dupes} | {d[1]["email"] for d in dupes}
        assert "john@work.com" in emails
        assert "john@personal.com" in emails

    def test_same_local_part_different_domain(self):
        devs = [
            {"name": "Alice A", "email": "alice@work.com", "commit_count": 30},
            {"name": "Alice B", "email": "alice@personal.com", "commit_count": 5},
        ]
        dupes = find_duplicates(devs)
        assert len(dupes) >= 1

    def test_normalized_name_match(self):
        devs = [
            {"name": "jane.smith", "email": "js@work.com", "commit_count": 20},
            {"name": "Jane Smith", "email": "jane@gmail.com", "commit_count": 5},
        ]
        dupes = find_duplicates(devs)
        assert len(dupes) >= 1

    def test_generic_names_excluded(self):
        devs = [
            {"name": "Admin", "email": "admin@server1.com", "commit_count": 5},
            {"name": "Admin", "email": "admin@server2.com", "commit_count": 3},
        ]
        dupes = find_duplicates(devs)
        assert len(dupes) == 0

    def test_root_excluded(self):
        devs = [
            {"name": "root", "email": "root@server1.com", "commit_count": 2},
            {"name": "root", "email": "root@server2.com", "commit_count": 1},
        ]
        dupes = find_duplicates(devs)
        assert len(dupes) == 0

    def test_no_duplicates(self):
        devs = [
            {"name": "Alice", "email": "alice@co.com", "commit_count": 50},
            {"name": "Bob", "email": "bob@co.com", "commit_count": 30},
            {"name": "Charlie", "email": "charlie@co.com", "commit_count": 20},
        ]
        dupes = find_duplicates(devs)
        assert len(dupes) == 0

    def test_empty_list(self):
        assert find_duplicates([]) == []

    def test_single_developer(self):
        devs = [{"name": "Alice", "email": "alice@co.com", "commit_count": 10}]
        assert find_duplicates(devs) == []

    def test_no_duplicate_pairs(self):
        """Same pair should not appear twice even if matched by multiple heuristics."""
        devs = [
            {"name": "John Doe", "email": "john.doe@work.com", "commit_count": 50},
            {"name": "John Doe", "email": "john.doe@personal.com", "commit_count": 10},
        ]
        dupes = find_duplicates(devs)
        # Should find the pair but only once
        email_pairs = {(d[0]["email"], d[1]["email"]) for d in dupes}
        assert len(email_pairs) == len(dupes)  # no duplicate pairs


class TestGenericNames:
    def test_known_generics(self):
        for name in ["admin", "root", "bot", "unknown", "system", "ci"]:
            assert name in GENERIC_NAMES or name.lower() in {n.lower() for n in GENERIC_NAMES}
