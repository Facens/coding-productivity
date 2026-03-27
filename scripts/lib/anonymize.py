"""
HMAC-SHA256 pseudonymization with file-based identity mapping.

Ported from the shared ``anonymize.py`` module but uses a local JSON
mapping file instead of BigQuery for reverse lookups.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Optional


HASH_TRUNCATION = 12  # hex characters (48 bits)


# ── Key management ────────────────────────────────────────────────────────────

def generate_salt() -> str:
    """Return a 32-byte cryptographically random hex string for use as a key."""
    return secrets.token_hex(32)


def load_key(key_hex: str) -> bytes:
    """Convert a hex-encoded key string to raw bytes."""
    return bytes.fromhex(key_hex)


# ── Hashing ───────────────────────────────────────────────────────────────────

def pseudonymize(value: str, key: bytes) -> str:
    """HMAC-SHA256 of *value* (lowercased), truncated to 12 hex chars."""
    normalized = value.strip().lower().encode("utf-8")
    digest = hmac.digest(key, normalized, digest=hashlib.sha256)
    return digest.hex()[:HASH_TRUNCATION]


# ── Merge resolution ─────────────────────────────────────────────────────────

def resolve_merge(value: str, merges: dict) -> str:
    """If *value* (lowercased) is an alias in *merges*, return the canonical.

    Otherwise return *value* as-is.
    """
    if not merges:
        return value
    lowered = value.strip().lower()
    return merges.get(lowered, value)


# ── Record hashing ────────────────────────────────────────────────────────────

def hash_record(
    record: dict,
    columns: list[str],
    key: bytes,
    merges: dict | None = None,
) -> dict:
    """Hash identity *columns* in *record*, resolving merges first.

    Returns the modified record (same dict, mutated in-place).
    """
    for col in columns:
        val = record.get(col)
        if not val:
            continue
        resolved = resolve_merge(val, merges) if merges else val
        record[col] = pseudonymize(resolved, key)
    return record


# ── File-based identity mapping ──────────────────────────────────────────────

def build_or_update_mapping(
    original_name: str,
    original_email: str,
    hashed_value: str,
    mapping_path: Path,
) -> None:
    """Add or update an entry in the local JSON identity mapping.

    The mapping is keyed by *hashed_value* and stores the original name and
    email for reverse lookup.  The file is written atomically and secured
    with ``chmod 600``.
    """
    mapping_path = Path(mapping_path)

    # Read existing mapping (or start fresh).
    if mapping_path.is_file():
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            mapping = {}
    else:
        mapping = {}

    mapping[hashed_value] = {
        "name": original_name,
        "email": original_email,
    }

    # Atomic write: .tmp then rename.
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = mapping_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(mapping_path))

    # Secure the file (owner read/write only).
    try:
        mapping_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def load_mapping(mapping_path: Path) -> dict:
    """Read the JSON identity mapping file and return it as a dict keyed by hash."""
    mapping_path = Path(mapping_path)
    if not mapping_path.is_file():
        return {}
    try:
        return json.loads(mapping_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(
            f"  Warning: could not read mapping file {mapping_path}",
            flush=True,
        )
        return {}
