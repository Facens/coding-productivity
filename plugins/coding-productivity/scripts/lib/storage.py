"""
Storage abstraction for the coding-productivity plugin.

Provides a common interface over DuckDB (local) and BigQuery (cloud) backends,
selected at runtime via the ``STORAGE_BACKEND`` configuration key.
"""

from __future__ import annotations

import abc
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import schema as _schema

_NOT_FOUND_NAMES = {"CatalogException", "NotFound"}


def is_table_missing(exc: BaseException) -> bool:
    """True only for "table (or dataset) does not exist" errors.

    DuckDB raises ``CatalogException``; BigQuery raises
    ``google.api_core.exceptions.NotFound``. Matched by class name so neither
    library has to be importable. Any other failure (network, quota,
    permissions) must propagate: treating it as "no such table" would make
    callers fall back to the wrong data.
    """
    return any(c.__name__ in _NOT_FOUND_NAMES for c in type(exc).__mro__)


# Seconds slept between retries of a streaming insert that BigQuery rejects
# because a table or column created moments ago is not visible yet.
_STREAM_RETRY_DELAYS = (2, 4, 8, 16, 32, 64, 120)  # ~4 minutes in total
_sleep = time.sleep

_NAMED_PARAM = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


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
    def list_tables(self) -> set[str]:
        """Names of all tables that currently exist in the store."""

    @abc.abstractmethod
    def ensure_table(self, table: str) -> None:
        """Create *table* (a known or versioned score table) if missing."""

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
            self._migrate_columns(
                list(_schema.SCHEMAS)
                + sorted(_schema.versioned_score_tables(self.list_tables()))
            )

    def _existing_columns(self, table: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = ?",
            [table],
        ).fetchall()
        return {r[0] for r in rows}

    def _migrate_columns(self, tables: list[str]) -> None:
        """Add columns that the current schema defines but the table lacks.

        Lets installs created by an older plugin version keep working: old
        rows get NULL in the new columns.
        """
        for table in tables:
            have = self._existing_columns(table)
            for col, dtype in _schema.get_column_types(table).items():
                if col not in have:
                    # Identifiers come from the schema whitelist, not user input.
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {dtype}"
                    )

    def list_tables(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
        return {r[0] for r in rows}

    def ensure_table(self, table: str) -> None:
        self._assert_writable()
        ddl = _schema.get_create_sql(table)  # validates the table name
        with self._lock:
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

        for table_name, columns in _schema.SCHEMAS.items():
            ref = self._table_ref(table_name)
            bq_schema = [
                bq.SchemaField(col, self._TYPE_MAP.get(dtype, "STRING"))
                for col, dtype in columns.items()
            ]
            table = bq.Table(ref, schema=bq_schema)
            self._client.create_table(table, exists_ok=True)

        self._migrate_columns(
            list(_schema.SCHEMAS)
            + sorted(_schema.versioned_score_tables(self.list_tables()))
        )

    _TYPE_MAP = {
        "VARCHAR": "STRING",
        "TEXT": "STRING",
        "INTEGER": "INTEGER",
        "DOUBLE": "FLOAT64",
        "TIMESTAMP": "TIMESTAMP",
        "BOOLEAN": "BOOLEAN",
    }

    def _migrate_columns(self, tables: list[str]) -> None:
        """Append schema columns missing from existing tables (nullable)."""
        from google.cloud import bigquery as bq

        for table_name in tables:
            table = self._client.get_table(self._table_ref(table_name))
            have = {f.name for f in table.schema}
            missing = [
                bq.SchemaField(col, self._TYPE_MAP.get(dtype, "STRING"), mode="NULLABLE")
                for col, dtype in _schema.get_column_types(table_name).items()
                if col not in have
            ]
            if missing:
                table.schema = list(table.schema) + missing
                self._client.update_table(table, ["schema"])

    def list_tables(self) -> set[str]:
        return {
            t.table_id
            for t in self._client.list_tables(f"{self._project}.{self._dataset}")
        }

    def ensure_table(self, table: str) -> None:
        self._assert_writable()
        from google.cloud import bigquery as bq

        bq_schema = [
            bq.SchemaField(col, self._TYPE_MAP.get(dtype, "STRING"))
            for col, dtype in _schema.get_column_types(table).items()
        ]
        self._client.create_table(
            bq.Table(self._table_ref(table), schema=bq_schema), exists_ok=True
        )

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
            self._stream_chunk(table, ref, clean[i : i + self._CHUNK_SIZE])

    def _stream_chunk(self, table: str, ref: str, chunk: list[dict]) -> None:
        """Stream one chunk, retrying while a fresh table or column is not
        yet visible to the streaming API ("no such field" / 404 right after
        ``create_table`` or ``update_table``)."""
        for attempt in range(len(_STREAM_RETRY_DELAYS) + 1):
            try:
                errors = self._client.insert_rows_json(ref, chunk)
            except Exception as exc:  # noqa: BLE001
                if not is_table_missing(exc):
                    raise
                errors, retryable = [str(exc)], True
            else:
                retryable = bool(errors) and "no such field" in str(errors).lower()
            if not errors and not retryable:
                return
            if not retryable or attempt == len(_STREAM_RETRY_DELAYS):
                raise RuntimeError(
                    f"BigQuery streaming insert errors for {table}: {errors}"
                )
            _sleep(_STREAM_RETRY_DELAYS[attempt])

    def query(self, sql: str, params: Optional[dict] = None) -> list[dict[str, Any]]:
        from google.cloud import bigquery as bq

        # Default dataset lets unqualified table names (as used by the
        # analysis and scoring SQL) resolve against the configured dataset.
        job_config = bq.QueryJobConfig(
            default_dataset=f"{self._project}.{self._dataset}"
        )
        if params:
            # The SQL in this plugin uses DuckDB's ``$name`` placeholders;
            # BigQuery wants ``@name`` and rejects ``$``.
            sql = _NAMED_PARAM.sub(
                lambda m: f"@{m.group(1)}" if m.group(1) in params else m.group(0), sql
            )
            job_config.query_parameters = [
                self._query_param(bq, k, v) for k, v in params.items()
            ]

        result = self._client.query(sql, job_config=job_config).result()
        return [dict(row) for row in result]

    @staticmethod
    def _query_param(bq, name: str, value: Any):
        """Typed query parameter. Strings stay STRING (dates included)."""
        def kind(v: Any) -> str:
            if isinstance(v, bool):
                return "BOOL"
            if isinstance(v, int):
                return "INT64"
            if isinstance(v, float):
                return "FLOAT64"
            return "STRING"

        if isinstance(value, (list, tuple, set)):
            values = list(value)
            return bq.ArrayQueryParameter(
                name, kind(values[0]) if values else "STRING", values
            )
        return bq.ScalarQueryParameter(name, kind(value), value)

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
        # Resolve relative DB_PATH against the config file's directory.
        db_path = Path(config.DB_PATH)
        if not db_path.is_absolute() and hasattr(config, "_path"):
            db_path = Path(config._path).parent / db_path
        return DuckDBStorage(str(db_path), readonly=readonly)

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
