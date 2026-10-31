"""
Where an incremental extraction starts, one repository at a time.

A single watermark for the whole store (its latest commit, of any repo) loses
history. A run that stops after some repos has already stored their newest
commits, so the next run starts every other repo from that date and skips
whatever those repos received in between, without an error.

So each repo starts from its own latest stored commit. A repo whose previous
run did not finish (its checkpoint is still there) restarts from the window
that run used, because its commits may have been stored newest first and its
latest stored commit says nothing about the older ones still missing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional


def _iso(value) -> Optional[str]:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_commit(storage, where: str = "", params: Optional[dict] = None) -> Optional[str]:
    """``MAX(committed_date)`` of the stored commits matching *where*, as
    ``YYYY-MM-DDTHH:MM:SSZ``; ``None`` when there is none or the table is missing."""
    sql = "SELECT MAX(committed_date) AS latest FROM commits"
    if where:
        sql += f" WHERE {where}"
    try:
        rows = storage.query(sql, params or None)
    except Exception:
        return None
    return _iso(rows[0].get("latest")) if rows else None


def resolve(repo_latest: Optional[str], store_latest: Optional[str],
            ckpt: Optional[dict]) -> tuple[Optional[str], str]:
    """``(since, why)`` for one repo of an incremental run (no ``--since``, no ``--full``).

    *ckpt* is the repo's checkpoint, if its last run did not finish. A
    ``run_since`` of ``""`` means that run had no lower bound.
    """
    if ckpt and "run_since" in ckpt:
        return ckpt["run_since"] or None, "resuming its unfinished run"
    if ckpt:
        # Left by a version that did not record its window.
        return repo_latest or store_latest, (
            "unfinished run of an earlier version: if its commits phase did not "
            "finish, rerun this repo with an explicit --since"
        )
    if repo_latest:
        return repo_latest, "latest commit of this repo"
    return store_latest, (
        "no commit of this repo in storage, so the latest commit in storage; "
        "extract it alone with --full for its history"
    )
