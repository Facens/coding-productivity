"""
Per-repo checkpoint persistence for resumable extraction.

Stores checkpoint files under ``.coding-productivity/checkpoints/`` so that
long-running ETL runs can be safely interrupted and resumed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_CHECKPOINT_DIR = Path(".coding-productivity") / "checkpoints"


def _slug(repo: str) -> str:
    """Convert a repo identifier (e.g. ``owner/repo``) to a filename-safe slug."""
    return repo.replace("/", "_")


def _checkpoint_path(repo_slug: str) -> Path:
    return _CHECKPOINT_DIR / f"{repo_slug}.json"


# ── Public API ────────────────────────────────────────────────────────────────


def load(repo_slug: str) -> Optional[dict]:
    """Load a saved checkpoint for *repo_slug*, or return ``None``.

    When an existing checkpoint is found a message is printed to stdout.
    """
    path = _checkpoint_path(repo_slug)
    if not path.is_file():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        print(
            f"  Resuming from checkpoint for {repo_slug} "
            f"(phase={data.get('phase')}, page={data.get('last_page')})...",
            flush=True,
        )
        return data
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"  Warning: corrupt checkpoint for {repo_slug}, ignoring ({exc})",
            flush=True,
        )
        return None


def save(repo_slug: str, state: dict) -> None:
    """Persist *state* for *repo_slug* atomically (write-then-rename)."""
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    state.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

    target = _checkpoint_path(repo_slug)
    tmp = target.with_suffix(".tmp")

    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(target))
    print(
        f"  Checkpoint saved for {repo_slug} (phase={state.get('phase')})",
        flush=True,
    )


def clear(repo_slug: str) -> None:
    """Delete the checkpoint for *repo_slug*, if it exists."""
    path = _checkpoint_path(repo_slug)
    try:
        path.unlink()
        print(f"  Checkpoint cleared for {repo_slug}", flush=True)
    except FileNotFoundError:
        pass


def exists(repo_slug: str) -> bool:
    """Return ``True`` if a checkpoint file exists for *repo_slug*."""
    return _checkpoint_path(repo_slug).is_file()
