"""
One row per merge request, whatever the store holds.

The extractors never update rows (BigQuery streaming inserts cannot be updated
right away), so a merge request whose state or merge commit changed is stored
again as a newer version, and stores written by older plugin versions hold the
same MR several times. ``mr_id`` (the platform's global MR/PR id) is the key;
the version with the latest ``extracted_at`` is the current one.
"""

from __future__ import annotations

from .duplicate_work import utc_naive

# The latest version of every MR. Use as ``FROM {LATEST_MRS} m``. MR rows
# without an ``mr_id`` (not written by the extractors) fall back to project
# name plus iid.
LATEST_MRS = """(
    SELECT * FROM (
        SELECT x.*, ROW_NUMBER() OVER (
            PARTITION BY COALESCE(NULLIF(x.mr_id, ''), CONCAT(x.project_name, '#', x.mr_iid))
            ORDER BY x.extracted_at DESC
        ) AS rn
        FROM merge_requests x
    ) t WHERE t.rn = 1
)"""


def stored_mrs(storage, where: str, params: dict) -> dict[str, dict]:
    """``{mr_id: latest row}`` for the MRs matching *where* (an SQL condition on ``m``)."""
    rows = storage.query(
        "SELECT m.mr_id AS mr_id, m.state AS state, m.updated_at AS updated_at, "
        "m.merge_commit_sha AS merge_commit_sha, m.squash_commit_sha AS squash_commit_sha "
        f"FROM {LATEST_MRS} m WHERE {where}",
        params,
    )
    return {str(r["mr_id"]): r for r in rows}


def _has_sha(row: dict) -> bool:
    return bool(row.get("merge_commit_sha") or row.get("squash_commit_sha"))


def needs_refresh(existing: dict | None, record: dict) -> bool:
    """Should *record* (just fetched) be stored next to the *existing* latest row?

    Yes when the MR is new, changed (``updated_at`` or ``state``), or now has a
    merge/squash sha the stored row lacks (rows from before 0.14). Otherwise
    storing it would only add a copy, so extraction is idempotent.
    """
    if existing is None:
        return True
    if utc_naive(existing["updated_at"]) != utc_naive(record.get("updated_at")):
        return True
    if existing["state"] != record.get("state"):
        return True
    return _has_sha(record) and not _has_sha(existing)
