"""
Storage abstraction for the coding-productivity plugin.

Provides a common interface over DuckDB (local) and BigQuery (cloud) backends,
selected at runtime via the ``STORAGE_BACKEND`` configuration key.
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import Any, Optional

from . import schema as _schema


# ── Abstract base ────────────────────────────────────────────────────────────

class Storage(abc.ABC):
    """Minimal contract every storage backend must satisfy."""

    @abc.abstractmethod
    def create_tables(self) -> None: ...

    @abc.abstractmethod
    def insert_batch(self, table: str, records: list[dict[str, Any]]) -> None: ...

    @abc.abstractmethod
    def query(self, sql: str, params: Optional[dict] = None) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    def get_existing_shas(self, table: str) -> set[str]: ...

    @abc.abstractmethod
    def count(self, table: str) -> int: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    # Context-manager support
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ── DuckDB implementation ───────────────────────────────────────────────────

class DuckDBStorage(Storage):
    """Local DuckDB storage with file-level locking for writes."""

    def __init__(self, db_path: str, *, readonly: bool = False):
        import duckdb
        from filelock import FileLock

        self._readonly = readonly
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # Lock file lives next to the database under .coding-productivity/
        lock_dir = self._db_path.parent
        self._lock = FileLock(str(lock_dir / "data.lock"))

        self._conn = duckdb.connect(str(self._db_path), read_only=readonly)

    def _assert_writable(self) -> None:
        if self._readonly:
            raise RuntimeError(
                "Storage is in read-only mode (STORAGE_MODE=readonly). "
                "Write operations are not permitted."
            )

    # ── Interface ────────────────────────────────────────────────────────

    def create_tables(self) -> None:
        self._assert_writable()
        with self._lock:
            for table_name in _schema.SCHEMAS:
                ddl = _schema.get_create_sql(table_name)
                self._conn.execute(ddl)

    def insert_batch(self, table: str, records: list[dict[str, Any]]) -> None:
        self._assert_writable()
        if not records:
            return

        columns = _schema.get_columns(table)
        placeholders = ", ".join(["?"] * len(columns))
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"

        rows = [
            tuple(record.get(col) for col in columns)
            for record in records
        ]

        with self._lock:
            self._conn.executemany(sql, rows)

    def query(self, sql: str, params: Optional[dict] = None) -> list[dict[str, Any]]:
        if params:
            # DuckDB supports named $param placeholders with a dict directly
            result = self._conn.execute(sql, params)
        else:
            result = self._conn.execute(sql)
        col_names = [desc[0] for desc in result.description]
        return [dict(zip(col_names, row)) for row in result.fetchall()]

    def get_existing_shas(self, table: str) -> set[str]:
        rows = self._conn.execute(
            f"SELECT DISTINCT commit_sha FROM {table}"
        ).fetchall()
        return {row[0] for row in rows}

    def count(self, table: str) -> int:
        result = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return result[0] if result else 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ── BigQuery implementation ──────────────────────────────────────────────────

class BigQueryStorage(Storage):
    """
    Google BigQuery storage backend.

    Requires the ``google-cloud-bigquery`` package::

        pip install google-cloud-bigquery
    """

    _CHUNK_SIZE = 1000  # streaming-insert batch size

    def __init__(
        self,
        project_id: str,
        dataset: str,
        *,
        credentials_path: str | None = None,
        readonly: bool = False,
    ):
        try:
            from google.cloud import bigquery as bq
        except ImportError:
            raise ImportError(
                "BigQuery backend requires the google-cloud-bigquery package.\n"
                "Install it with:  pip install google-cloud-bigquery"
            ) from None

        self._readonly = readonly
        self._project = project_id
        self._dataset = dataset

        if credentials_path:
            import os
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path

        self._client = bq.Client(project=project_id)

    def _table_ref(self, table: str) -> str:
        return f"{self._project}.{self._dataset}.{table}"

    def _assert_writable(self) -> None:
        if self._readonly:
            raise RuntimeError(
                "Storage is in read-only mode (STORAGE_MODE=readonly). "
                "Write operations are not permitted."
            )

    # ── Interface ────────────────────────────────────────────────────────

    def create_tables(self) -> None:
        self._assert_writable()
        from google.cloud import bigquery as bq

        # Map DuckDB types to BigQuery types
        type_map = {
            "VARCHAR": "STRING",
            "TEXT": "STRING",
            "INTEGER": "INTEGER",
            "DOUBLE": "FLOAT64",
            "TIMESTAMP": "TIMESTAMP",
            "BOOLEAN": "BOOLEAN",
        }

        for table_name, columns in _schema.SCHEMAS.items():
            ref = self._table_ref(table_name)
            bq_schema = [
                bq.SchemaField(col, type_map.get(dtype, "STRING"))
                for col, dtype in columns.items()
            ]
            table = bq.Table(ref, schema=bq_schema)
            self._client.create_table(table, exists_ok=True)

    def insert_batch(self, table: str, records: list[dict[str, Any]]) -> None:
        self._assert_writable()
        if not records:
            return

        ref = self._table_ref(table)
        columns = _schema.get_columns(table)

        # Normalize records to only include schema columns.
        clean = [
            {col: rec.get(col) for col in columns}
            for rec in records
        ]

        # Stream in chunks.
        for i in range(0, len(clean), self._CHUNK_SIZE):
            chunk = clean[i : i + self._CHUNK_SIZE]
            errors = self._client.insert_rows_json(ref, chunk)
            if errors:
                raise RuntimeError(
                    f"BigQuery streaming insert errors for {table}: {errors}"
                )

    def query(self, sql: str, params: Optional[dict] = None) -> list[dict[str, Any]]:
        from google.cloud import bigquery as bq

        job_config = None
        if params:
            query_params = [
                bq.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()
            ]
            job_config = bq.QueryJobConfig(query_parameters=query_params)

        result = self._client.query(sql, job_config=job_config).result()
        return [dict(row) for row in result]

    def get_existing_shas(self, table: str) -> set[str]:
        ref = self._table_ref(table)
        rows = self.query(f"SELECT DISTINCT commit_sha FROM `{ref}`")
        return {row["commit_sha"] for row in rows}

    def count(self, table: str) -> int:
        ref = self._table_ref(table)
        rows = self.query(f"SELECT COUNT(*) AS cnt FROM `{ref}`")
        return rows[0]["cnt"] if rows else 0

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


# ── Factory ──────────────────────────────────────────────────────────────────

def get_storage(config) -> Storage:
    """
    Return the appropriate Storage implementation based on *config*.

    *config* should be an instance of ``lib.config.Config`` (or any object
    with the same attribute interface).
    """
    backend = (config.STORAGE_BACKEND or "").lower()
    readonly = (config.STORAGE_MODE or "").lower() == "readonly"

    if backend == "duckdb":
        if not config.DB_PATH:
            raise ValueError("DB_PATH must be set for the duckdb backend.")
        return DuckDBStorage(config.DB_PATH, readonly=readonly)

    if backend == "bigquery":
        if not config.GCP_PROJECT_ID or not config.BQ_DATASET:
            raise ValueError(
                "GCP_PROJECT_ID and BQ_DATASET must be set for the bigquery backend."
            )
        return BigQueryStorage(
            project_id=config.GCP_PROJECT_ID,
            dataset=config.BQ_DATASET,
            credentials_path=config.GOOGLE_APPLICATION_CREDENTIALS,
            readonly=readonly,
        )

    raise ValueError(
        f"Unknown STORAGE_BACKEND: '{backend}'. Use 'duckdb' or 'bigquery'."
    )
