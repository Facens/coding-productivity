"""
Identity deduplication for the coding-productivity plugin.

Detects duplicate developer identities across different email addresses,
merges them into a canonical identity, and retroactively updates stored
hashes so historical data remains consistent.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from . import anonymize
from . import schema as _schema


# ── Generic names to skip ────────────────────────────────────────────────────

GENERIC_NAMES: set[str] = frozenset({
    "admin",
    "administrator",
    "bot",
    "ci",
    "noreply",
    "no-reply",
    "root",
    "system",
    "unknown",
})


# ── Name normalization ───────────────────────────────────────────────────────

def normalize_name(name: str) -> str:
    """Lowercase, strip whitespace, remove dots, hyphens, and underscores."""
    return (
        name.strip()
        .lower()
        .replace(".", "")
        .replace("-", "")
        .replace("_", "")
    )


def _is_generic(name: str) -> bool:
    """Return True if *name* normalizes to a known generic identity."""
    return normalize_name(name) in GENERIC_NAMES


# ── Duplicate detection ─────────────────────────────────────────────────────

def find_duplicates(
    developers: list[dict],
) -> list[tuple[dict, dict, str]]:
    """Find candidate duplicate developer identities.

    Parameters
    ----------
    developers:
        List of dicts, each with keys ``name``, ``email``, ``commit_count``.

    Returns
    -------
    list[tuple[dict, dict, str]]
        Each tuple is ``(developer_a, developer_b, reason)`` describing a
        suspected duplicate pair.  The list is empty when no candidates are
        found.

    Heuristics (applied in order, earlier matches take priority):
      1. Exact name match (case-insensitive) with different emails.
      2. Same email local-part across different domains.
      3. Normalized name match (strip dots, hyphens, underscores) with
         different emails.
    """
    if not developers:
        return []

    results: list[tuple[dict, dict, str]] = []
    seen_pairs: set[frozenset[str]] = set()

    def _add(a: dict, b: dict, reason: str) -> None:
        pair_key = frozenset((a["email"].lower(), b["email"].lower()))
        if pair_key not in seen_pairs:
            seen_pairs.add(pair_key)
            results.append((a, b, reason))

    # Pre-filter: drop entries with generic names.
    valid = [d for d in developers if not _is_generic(d.get("name", ""))]

    # --- Heuristic 1: exact name match (case-insensitive), different emails --
    by_lower_name: dict[str, list[dict]] = {}
    for dev in valid:
        key = dev["name"].strip().lower()
        by_lower_name.setdefault(key, []).append(dev)

    for _name, group in by_lower_name.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if a["email"].lower() != b["email"].lower():
                    _add(a, b, "exact_name_match")

    # --- Heuristic 2: same email local-part, different domains ---------------
    by_local: dict[str, list[dict]] = {}
    for dev in valid:
        email = dev.get("email", "")
        if "@" not in email:
            continue
        local = email.split("@", 1)[0].strip().lower()
        if not local:
            continue
        by_local.setdefault(local, []).append(dev)

    for _local, group in by_local.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if a["email"].lower() != b["email"].lower():
                    _add(a, b, "same_local_part")

    # --- Heuristic 3: normalized name match ----------------------------------
    by_normalized: dict[str, list[dict]] = {}
    for dev in valid:
        key = normalize_name(dev["name"])
        if not key:
            continue
        by_normalized.setdefault(key, []).append(dev)

    for _norm, group in by_normalized.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if a["email"].lower() != b["email"].lower():
                    _add(a, b, "normalized_name_match")

    return results


# ── Identity merging ────────────────────────────────────────────────────────

def merge_identities(
    canonical_email: str,
    alias_emails: list[str],
    config,
) -> None:
    """Register *alias_emails* as aliases of *canonical_email* in config.

    Updates ``config.IDENTITY_MERGES`` by appending new ``alias:canonical``
    pairs and persists the change via ``config.update()``.
    """
    existing = config.IDENTITY_MERGES  # dict[str, str]

    # Build the updated serialized value.
    merged = dict(existing)
    for alias in alias_emails:
        alias_lower = alias.strip().lower()
        canonical_lower = canonical_email.strip().lower()
        if alias_lower and alias_lower != canonical_lower:
            merged[alias_lower] = canonical_lower

    # Serialize back to the ``alias1:canonical1,alias2:canonical2`` format.
    serialized = ",".join(f"{a}:{c}" for a, c in merged.items())
    config.update("IDENTITY_MERGES", serialized)


# ── Retroactive hash rewriting ──────────────────────────────────────────────

# Columns that may contain pseudonymized identity hashes, per table.
_IDENTITY_COLUMNS: dict[str, list[str]] = {}

for _table_name, _cols in _schema.SCHEMAS.items():
    _id_cols = [
        c
        for c in _cols
        if c in ("author_name", "author_email", "committer_name", "committer_email")
    ]
    if _id_cols:
        _IDENTITY_COLUMNS[_table_name] = _id_cols


def apply_retroactive_merges(
    storage,
    mapping_path: Path,
    key: bytes,
    new_merges: dict[str, str],
) -> int:
    """Rewrite stored hashes so aliases point to their canonical identity.

    Parameters
    ----------
    storage:
        An open ``Storage`` instance (must be writable).
    mapping_path:
        Path to the JSON identity-mapping file used by ``anonymize``.
    key:
        The raw HMAC key (bytes) used for pseudonymization.
    new_merges:
        ``{alias_email: canonical_email}`` pairs to apply.

    Returns
    -------
    int
        Total number of database rows affected across all tables.

    Notes
    -----
    This operation is **idempotent**: running it twice with the same
    *new_merges* produces the same result because UPDATEs are
    ``SET col = new WHERE col = old`` -- once the old value is gone the
    UPDATE simply matches zero rows.
    """
    if not new_merges:
        return 0

    mapping = anonymize.load_mapping(mapping_path)

    total_affected = 0

    for alias_email, canonical_email in new_merges.items():
        alias_lower = alias_email.strip().lower()
        canonical_lower = canonical_email.strip().lower()

        if alias_lower == canonical_lower:
            continue

        # Find the old hash for the alias email in the mapping file.
        old_hash = None
        for h, info in mapping.items():
            if info.get("email", "").strip().lower() == alias_lower:
                old_hash = h
                break

        if old_hash is None:
            # Alias was never hashed -- nothing to rewrite.
            continue

        # Compute the new canonical hash.
        new_hash = anonymize.pseudonymize(canonical_lower, key)

        if old_hash == new_hash:
            continue

        # Update every identity column in every applicable table.
        for table, columns in _IDENTITY_COLUMNS.items():
            for col in columns:
                sql = f"UPDATE {table} SET {col} = ? WHERE {col} = ?"
                rows = storage.query(
                    f"SELECT COUNT(*) AS cnt FROM {table} WHERE {col} = ?",
                    {"v": old_hash},
                )
                count = rows[0]["cnt"] if rows else 0

                if count > 0:
                    storage.query(sql, {"new": new_hash, "old": old_hash})
                    total_affected += count

        # Update the mapping file: add new canonical entry, mark old as merged.
        # Look up the canonical identity's original name from the mapping
        # (prefer the alias's name if no canonical entry exists yet).
        canonical_info = None
        for h, info in mapping.items():
            if info.get("email", "").strip().lower() == canonical_lower:
                canonical_info = info
                break

        canonical_name = (
            canonical_info["name"] if canonical_info else mapping[old_hash]["name"]
        )

        anonymize.build_or_update_mapping(
            original_name=canonical_name,
            original_email=canonical_lower,
            hashed_value=new_hash,
            mapping_path=mapping_path,
        )

        # Reload mapping after update and mark old hash as merged.
        mapping = anonymize.load_mapping(mapping_path)
        if old_hash in mapping:
            mapping[old_hash]["merged_into"] = new_hash
            # Write back atomically.
            mapping_path = Path(mapping_path)
            tmp = mapping_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(mapping, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(mapping_path))
            try:
                mapping_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    return total_affected
