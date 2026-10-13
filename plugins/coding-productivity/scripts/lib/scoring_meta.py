"""
Scoring series bookkeeping: which versioned score tables are active.

The scoring model is a measuring instrument: one series of scores is produced
by one model and one prompt. Switching models means rescoring everything into
a new series (``commit_scores_v{N}`` / ``file_scores_v{N}``) and then switching
the *active* series, never mixing models inside one table.

Layout
------
* Version 1 is the legacy, unversioned pair ``commit_scores`` / ``file_scores``.
* Versions >= 2 live in ``commit_scores_v{N}`` / ``file_scores_v{N}``.
* ``scoring_meta`` is an append-only log (BigQuery streaming inserts cannot be
  updated right away, so rows are never modified). The active series is the
  most recent row with ``status = 'active'``. With no such row, the legacy
  tables are active.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

LEGACY_VERSION = 1

_VERSIONED_RE = re.compile(r"^(?:commit|file)_scores_v([0-9]+)$")


def table_names(version: int) -> tuple[str, str]:
    """Return ``(commit_table, file_table)`` for a series version."""
    if version < 1:
        raise ValueError(f"Invalid scoring version: {version}")
    if version == LEGACY_VERSION:
        return "commit_scores", "file_scores"
    return f"commit_scores_v{version}", f"file_scores_v{version}"


@dataclass(frozen=True)
class ActiveScoring:
    version: int
    commit_table: str
    file_table: str
    scorer_model: Optional[str]  # configured model recorded at activation
    prompt_version: Optional[str]
    from_meta: bool  # False = legacy default, no scoring_meta row


def _legacy() -> ActiveScoring:
    commit_table, file_table = table_names(LEGACY_VERSION)
    return ActiveScoring(LEGACY_VERSION, commit_table, file_table, None, None, False)


def get_active(storage) -> ActiveScoring:
    """Return the active scoring series (legacy tables when none is recorded)."""
    try:
        rows = storage.query(
            "SELECT version, commit_table, file_table, scorer_model, prompt_version "
            "FROM scoring_meta WHERE status = 'active' "
            "ORDER BY recorded_at DESC, version DESC LIMIT 1"
        )
    except Exception:
        # No scoring_meta table yet (older install or read-only connection).
        return _legacy()
    if not rows:
        return _legacy()
    r = rows[0]
    version = int(r["version"])
    commit_table, file_table = table_names(version)
    return ActiveScoring(
        version, commit_table, file_table,
        r.get("scorer_model"), r.get("prompt_version"), True,
    )


def next_free_version(storage, reserved: tuple[int, ...] = ()) -> int:
    """Smallest N >= 2 above every existing table, meta row and *reserved* N."""
    seen = set(reserved)
    for name in storage.list_tables():
        m = _VERSIONED_RE.match(name)
        if m:
            seen.add(int(m.group(1)))
    try:
        for r in storage.query("SELECT DISTINCT version FROM scoring_meta"):
            seen.add(int(r["version"]))
    except Exception:
        pass
    return max(seen | {LEGACY_VERSION}) + 1


def record(storage, version: int, status: str, scorer_model: str,
           prompt_version: str) -> None:
    """Append a ``building`` or ``active`` row to ``scoring_meta``."""
    if status not in ("building", "active"):
        raise ValueError(f"Invalid scoring_meta status: {status}")
    commit_table, file_table = table_names(version)
    storage.insert_batch("scoring_meta", [{
        "version": version,
        "status": status,
        "commit_table": commit_table,
        "file_table": file_table,
        "scorer_model": scorer_model,
        "prompt_version": prompt_version,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }])


def model_matches(configured: Optional[str], returned: Optional[str]) -> bool:
    """True if *returned* (``response.model``) is the *configured* model.

    The API may resolve an alias to a dated snapshot, so
    ``claude-haiku-5-5`` matches ``claude-haiku-5-5-20260301``.
    """
    if not configured or not returned:
        return False
    if returned == configured:
        return True
    # Only a date suffix counts as "the same model": claude-haiku-5 must not
    # match claude-haiku-5-5.
    return bool(re.fullmatch(re.escape(configured) + r"-\d{8}", returned))
