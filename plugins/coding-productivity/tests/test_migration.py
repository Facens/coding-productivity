"""Schema migration: provenance columns are added to pre-existing tables."""

import sys
import types
from collections import namedtuple

import pytest

from lib import schema
from lib.storage import DuckDBStorage

OLD_COMMIT_SCORES = (
    "CREATE TABLE commit_scores (commit_sha VARCHAR, total_productivity DOUBLE, "
    "weighted_score DOUBLE, overall_category VARCHAR, scored_at TIMESTAMP)"
)
OLD_FILE_SCORES = (
    "CREATE TABLE file_scores (commit_sha VARCHAR, file_path VARCHAR, score DOUBLE, "
    "category VARCHAR, reasoning TEXT, scored_at TIMESTAMP)"
)
NEW_COLUMNS = {"scorer_model", "prompt_version"}


def _columns(storage, table):
    rows = storage.query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = $t",
        {"t": table},
    )
    return {r["column_name"] for r in rows}


def test_provenance_columns_in_schema():
    for table in ("commit_scores", "file_scores"):
        assert NEW_COLUMNS <= set(schema.get_columns(table))


def test_duckdb_create_tables_adds_columns_to_existing_tables(tmp_dir):
    storage = DuckDBStorage(str(tmp_dir / "old.duckdb"))
    storage._conn.execute(OLD_COMMIT_SCORES)
    storage._conn.execute(OLD_FILE_SCORES)
    storage._conn.execute(
        "INSERT INTO commit_scores VALUES ('abc', 1.0, 0.5, 'feature', '2026-01-01')"
    )
    assert not NEW_COLUMNS & _columns(storage, "commit_scores")

    storage.create_tables()

    assert NEW_COLUMNS <= _columns(storage, "commit_scores")
    assert NEW_COLUMNS <= _columns(storage, "file_scores")
    # Old rows survive with NULL provenance.
    row = storage.query("SELECT * FROM commit_scores")[0]
    assert row["commit_sha"] == "abc"
    assert row["scorer_model"] is None and row["prompt_version"] is None
    # And new rows can carry it.
    storage.insert_batch("commit_scores", [{
        "commit_sha": "def", "total_productivity": 2.0, "weighted_score": 0.4,
        "overall_category": "bugfix", "scored_at": "2026-02-01T00:00:00Z",
        "scorer_model": "claude-haiku-5-5", "prompt_version": "abc123abc123",
    }])
    assert storage.count("commit_scores") == 2
    storage.close()


def test_duckdb_migration_is_idempotent(duckdb_storage):
    duckdb_storage.create_tables()
    duckdb_storage.create_tables()
    assert NEW_COLUMNS <= _columns(duckdb_storage, "commit_scores")


def test_duckdb_migrates_versioned_tables_too(tmp_dir):
    storage = DuckDBStorage(str(tmp_dir / "v.duckdb"))
    storage.create_tables()
    storage._conn.execute(OLD_COMMIT_SCORES.replace("commit_scores", "commit_scores_v2"))
    storage.create_tables()
    assert NEW_COLUMNS <= _columns(storage, "commit_scores_v2")
    storage.close()


def test_versioned_tables_share_base_schema(duckdb_storage):
    assert schema.get_columns("commit_scores_v7") == schema.get_columns("commit_scores")
    duckdb_storage.ensure_table("file_scores_v7")
    assert "file_scores_v7" in duckdb_storage.list_tables()
    duckdb_storage.insert_batch("commit_scores", [])  # sanity: base still works


def test_table_names_are_whitelisted():
    for bad in ("commits; DROP TABLE commits", "commit_scores_v", "other_v2", "commit_scores_vx"):
        with pytest.raises(ValueError):
            schema.base_table(bad)


# ── BigQuery: same migration through a fake client ───────────────────────────

Field = namedtuple("Field", "name field_type mode", defaults=("NULLABLE",))


class _FakeTable:
    def __init__(self, ref, schema=None):
        self.ref, self.schema = ref, list(schema or [])


class _FakeBQClient:
    def __init__(self, project=None):
        self.tables = {}
        self.updates = []

    def create_table(self, table, exists_ok=False):
        self.tables.setdefault(table.ref, table)

    def get_table(self, ref):
        return self.tables[ref]

    def update_table(self, table, fields):
        self.updates.append((table.ref, fields))

    def list_tables(self, dataset):
        return [types.SimpleNamespace(table_id=r.split(".")[-1]) for r in self.tables]

    def close(self):
        pass


def test_bigquery_create_tables_appends_missing_columns(monkeypatch):
    fake_bq = types.SimpleNamespace(Client=_FakeBQClient, Table=_FakeTable, SchemaField=Field)
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.bigquery = fake_bq
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bq)

    from lib.storage import BigQueryStorage

    storage = BigQueryStorage("proj", "ds")
    old_ref = "proj.ds.commit_scores"
    storage._client.tables[old_ref] = _FakeTable(old_ref, [
        Field("commit_sha", "STRING"), Field("total_productivity", "FLOAT64"),
        Field("weighted_score", "FLOAT64"), Field("overall_category", "STRING"),
        Field("scored_at", "TIMESTAMP"),
    ])

    storage.create_tables()

    names = {f.name for f in storage._client.tables[old_ref].schema}
    assert NEW_COLUMNS <= names
    assert all(f.mode == "NULLABLE" for f in storage._client.tables[old_ref].schema)
    assert storage._client.updates == [(old_ref, ["schema"])]  # only the stale table
