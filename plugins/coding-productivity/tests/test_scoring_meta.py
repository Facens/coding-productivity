"""Active-series bookkeeping (lib/scoring_meta.py)."""

from lib import scoring_meta as sm


def test_table_names():
    assert sm.table_names(1) == ("commit_scores", "file_scores")
    assert sm.table_names(3) == ("commit_scores_v3", "file_scores_v3")


def test_legacy_is_active_without_meta_rows(duckdb_storage):
    active = sm.get_active(duckdb_storage)
    assert (active.version, active.commit_table, active.from_meta) == (1, "commit_scores", False)


def test_legacy_when_meta_table_missing(tmp_dir):
    from lib.storage import DuckDBStorage
    storage = DuckDBStorage(str(tmp_dir / "bare.duckdb"))  # no create_tables()
    assert sm.get_active(storage).version == 1
    storage.close()


def test_building_rows_do_not_activate_and_latest_active_wins(duckdb_storage):
    sm.record(duckdb_storage, 2, "building", "claude-haiku-5-5", "aaaaaaaaaaaa")
    assert sm.get_active(duckdb_storage).version == 1

    sm.record(duckdb_storage, 2, "active", "claude-haiku-5-5", "aaaaaaaaaaaa")
    active = sm.get_active(duckdb_storage)
    assert (active.version, active.commit_table, active.scorer_model) == (
        2, "commit_scores_v2", "claude-haiku-5-5")

    sm.record(duckdb_storage, 3, "active", "claude-haiku-6", "bbbbbbbbbbbb")
    assert sm.get_active(duckdb_storage).version == 3


def test_next_free_version_counts_tables_and_meta(duckdb_storage):
    assert sm.next_free_version(duckdb_storage) == 2
    duckdb_storage.ensure_table("commit_scores_v2")
    assert sm.next_free_version(duckdb_storage) == 3
    sm.record(duckdb_storage, 5, "building", "m", "p")
    assert sm.next_free_version(duckdb_storage) == 6


def test_model_matches_resolves_aliases_to_snapshots():
    assert sm.model_matches("claude-haiku-5-5", "claude-haiku-5-5")
    assert sm.model_matches("claude-haiku-5-5", "claude-haiku-5-5-20260301")
    assert not sm.model_matches("claude-haiku-5", "claude-haiku-5-5")
    assert not sm.model_matches("claude-haiku-5-5", None)
