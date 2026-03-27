"""
Configuration loader for .coding-productivity.env files.

Searches for the config file in the current directory and parent directories,
exposes typed accessor properties for every supported key, and provides
validation and serialization helpers.
"""

from __future__ import annotations

import os
import stat
import warnings
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values, set_key


CONFIG_FILENAME = ".coding-productivity.env"


def _find_config(start: Path | None = None) -> Path | None:
    """Walk *start* and its parents looking for CONFIG_FILENAME."""
    directory = Path(start) if start else Path.cwd()
    if directory.is_file():
        directory = directory.parent
    for parent in [directory, *directory.parents]:
        candidate = parent / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


class Config:
    """Typed, validated access to .coding-productivity.env settings."""

    def __init__(self, path: str | Path | None = None):
        if path is not None:
            self._path = Path(path)
        else:
            found = _find_config()
            if found is None:
                raise FileNotFoundError(
                    f"Could not locate {CONFIG_FILENAME} in the current "
                    "directory or any parent directory."
                )
            self._path = found

        self._check_permissions()
        self._data: dict[str, str | None] = dotenv_values(self._path)

    # ── Permission check ─────────────────────────────────────────────────

    def _check_permissions(self) -> None:
        """Warn (but continue) if the config file is group/world-readable."""
        try:
            mode = self._path.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                warnings.warn(
                    f"{self._path} is readable by group/others. "
                    "Consider running: chmod 600 "
                    f'"{self._path}"',
                    stacklevel=2,
                )
        except OSError:
            pass

    # ── Raw helpers ──────────────────────────────────────────────────────

    def _get(self, key: str, default: str | None = None) -> str | None:
        return self._data.get(key, default)

    def _get_bool(self, key: str, default: bool = False) -> bool:
        val = self._data.get(key)
        if val is None:
            return default
        return val.strip().lower() in ("1", "true", "yes", "on")

    def _get_list(self, key: str) -> list[str]:
        val = self._data.get(key)
        if not val:
            return []
        return [item.strip() for item in val.split(",") if item.strip()]

    def _get_dict(self, key: str) -> dict[str, str]:
        """Parse ``alias1:canonical1,alias2:canonical2`` into a dict."""
        val = self._data.get(key)
        if not val:
            return {}
        result: dict[str, str] = {}
        for pair in val.split(","):
            pair = pair.strip()
            if ":" not in pair:
                continue
            alias, canonical = pair.split(":", 1)
            alias, canonical = alias.strip(), canonical.strip()
            if alias and canonical:
                result[alias] = canonical
        return result

    # ── Typed accessors ──────────────────────────────────────────────────

    @property
    def PLATFORM(self) -> str | None:
        return self._get("PLATFORM")

    @property
    def GITHUB_TOKEN(self) -> str | None:
        return self._get("GITHUB_TOKEN")

    @property
    def GITLAB_TOKEN(self) -> str | None:
        return self._get("GITLAB_TOKEN")

    @property
    def GITLAB_URL(self) -> str | None:
        return self._get("GITLAB_URL")

    @property
    def GITLAB_CA_BUNDLE(self) -> str | None:
        return self._get("GITLAB_CA_BUNDLE")

    @property
    def REPOS(self) -> list[str]:
        return self._get_list("REPOS")

    @property
    def STORAGE_BACKEND(self) -> str | None:
        return self._get("STORAGE_BACKEND")

    @property
    def STORAGE_MODE(self) -> str | None:
        return self._get("STORAGE_MODE")

    @property
    def DB_PATH(self) -> str | None:
        return self._get("DB_PATH")

    @property
    def GCP_PROJECT_ID(self) -> str | None:
        return self._get("GCP_PROJECT_ID")

    @property
    def BQ_DATASET(self) -> str | None:
        return self._get("BQ_DATASET")

    @property
    def GOOGLE_APPLICATION_CREDENTIALS(self) -> str | None:
        return self._get("GOOGLE_APPLICATION_CREDENTIALS")

    @property
    def SCORING_ENABLED(self) -> bool:
        return self._get_bool("SCORING_ENABLED")

    @property
    def ANTHROPIC_API_KEY(self) -> str | None:
        return self._get("ANTHROPIC_API_KEY")

    @property
    def ANONYMIZATION_ENABLED(self) -> bool:
        return self._get_bool("ANONYMIZATION_ENABLED")

    @property
    def PSEUDONYMIZATION_KEY(self) -> bytes | None:
        val = self._get("PSEUDONYMIZATION_KEY")
        if val is None:
            return None
        return val.encode("utf-8")

    @property
    def EXCLUDED_DEVELOPERS(self) -> list[str]:
        return self._get_list("EXCLUDED_DEVELOPERS")

    @property
    def BOT_OVERRIDES(self) -> list[str]:
        return self._get_list("BOT_OVERRIDES")

    @property
    def IDENTITY_MERGES(self) -> dict[str, str]:
        return self._get_dict("IDENTITY_MERGES")

    # ── Mutation ─────────────────────────────────────────────────────────

    def update(self, key: str, value: str) -> None:
        """Update a single key in-memory and on disk."""
        self._data[key] = value
        set_key(str(self._path), key, value)

    # ── Validation ───────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """
        Check that required fields are set based on enabled features.

        Returns a list of human-readable error strings (empty = valid).
        """
        errors: list[str] = []

        platform = (self.PLATFORM or "").lower()
        if not platform:
            errors.append("PLATFORM is required (github or gitlab).")
        elif platform not in ("github", "gitlab"):
            errors.append(f"PLATFORM must be 'github' or 'gitlab', got '{platform}'.")

        if platform == "github" and not self.GITHUB_TOKEN:
            errors.append("GITHUB_TOKEN is required when PLATFORM=github.")
        if platform == "gitlab" and not self.GITLAB_TOKEN:
            errors.append("GITLAB_TOKEN is required when PLATFORM=gitlab.")
        if platform == "gitlab" and not self.GITLAB_URL:
            errors.append("GITLAB_URL is required when PLATFORM=gitlab.")

        backend = (self.STORAGE_BACKEND or "").lower()
        if not backend:
            errors.append("STORAGE_BACKEND is required (duckdb or bigquery).")
        elif backend == "duckdb" and not self.DB_PATH:
            errors.append("DB_PATH is required when STORAGE_BACKEND=duckdb.")
        elif backend == "bigquery":
            if not self.GCP_PROJECT_ID:
                errors.append("GCP_PROJECT_ID is required when STORAGE_BACKEND=bigquery.")
            if not self.BQ_DATASET:
                errors.append("BQ_DATASET is required when STORAGE_BACKEND=bigquery.")

        if self.SCORING_ENABLED and not self.ANTHROPIC_API_KEY:
            errors.append("ANTHROPIC_API_KEY is required when SCORING_ENABLED=true.")

        if self.ANONYMIZATION_ENABLED and not self.PSEUDONYMIZATION_KEY:
            errors.append(
                "PSEUDONYMIZATION_KEY is required when ANONYMIZATION_ENABLED=true."
            )

        return errors

    # ── Storage presence check ───────────────────────────────────────────

    def has_data(self, storage: "Storage") -> bool:  # noqa: F821
        """Return True if the given storage already contains commit data."""
        return storage.count("commits") > 0

    # ── Serialization ────────────────────────────────────────────────────

    def write(self, path: str | Path) -> None:
        """
        Write current configuration to *path* as an env file.

        Preserves the original file's comments and ordering when possible;
        appends new keys at the end.
        """
        target = Path(path)
        existing_lines: list[str] = []
        written_keys: set[str] = set()

        # Preserve existing lines (including comments) if the file exists.
        if target.exists():
            existing_lines = target.read_text().splitlines(keepends=True)

        output_lines: list[str] = []
        for line in existing_lines:
            stripped = line.strip()
            if stripped.startswith("#") or not stripped or "=" not in stripped:
                output_lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in self._data:
                val = self._data[key]
                output_lines.append(f"{key}={val}\n" if val is not None else f"{key}=\n")
                written_keys.add(key)
            else:
                output_lines.append(line)

        # Append keys that were not already in the file.
        for key, val in self._data.items():
            if key not in written_keys and val is not None:
                output_lines.append(f"{key}={val}\n")

        target.write_text("".join(output_lines))

        # Secure permissions (owner-only read/write).
        try:
            target.chmod(0o600)
        except OSError:
            pass

    # ── repr ─────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"<Config path={self._path}>"
