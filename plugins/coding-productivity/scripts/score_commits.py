#!/usr/bin/env python3
"""
Score commits using Claude Haiku to measure intellectual value per file.

Reads unscored commits + diffs from the configured storage backend, sends each
file's diff to Claude for evaluation, and writes results to the commit_scores
and file_scores tables.

Usage::

    python score_commits.py --config /path/to/.coding-productivity.env
    python score_commits.py --config .coding-productivity.env --workers 5 --batch-size 25

"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Put scripts/lib on the import path so ``from lib import ...`` works when
# the script is invoked directly.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from lib.config import Config
from lib.storage import get_storage, Storage

# ---------------------------------------------------------------------------
# Conditional import for Anthropic SDK
# ---------------------------------------------------------------------------
try:
    import anthropic
except ImportError:
    print(
        "Error: the 'anthropic' package is required for commit scoring.\n"
        "Install it with:  pip install anthropic",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONSENT_FILE = Path(".coding-productivity") / ".scoring_consent"

VALID_CATEGORIES = {
    "feature", "bugfix", "refactor", "test", "docs", "style",
    "perf", "security", "deps", "config", "chore", "localization",
}

# Files auto-scored without an API call (pattern -> score).
AUTO_SCORE_PATTERNS: dict[str, float] = {
    # Lockfiles
    r".*\.lock$": 0.0,
    r"yarn\.lock$": 0.0,
    r"Gemfile\.lock$": 0.0,
    r"package-lock\.json$": 0.0,
    r"pnpm-lock\.yaml$": 0.0,
    r"composer\.lock$": 0.0,
    r"Pipfile\.lock$": 0.0,
    r"poetry\.lock$": 0.0,
    r"Cargo\.lock$": 0.0,
    # Minified files
    r".*\.min\.js$": 0.0,
    r".*\.min\.css$": 0.0,
    # Source maps
    r".*\.map$": 0.02,
    # Generated / build output
    r"^generated/.*": 0.0,
    r"^dist/.*": 0.0,
    r"^build/.*": 0.0,
    r".*/generated/.*": 0.0,
    r".*/dist/.*": 0.0,
    r".*/build/.*": 0.0,
    # Localization / translation files
    r".*/locales/.*\.yml$": 0.05,
    r".*/locales/.*\.yaml$": 0.05,
    r".*/i18n/.*\.yml$": 0.05,
    r".*/i18n/.*\.json$": 0.05,
    r".*/translations/.*": 0.05,
    # Config
    r"\.gitignore$": 0.1,
}

# Post-processing score caps by extension.
MAX_SCORE_BY_EXTENSION: dict[str, float] = {
    ".md": 0.15,
}

# Large JSON files (> threshold lines, excluding package.json) are capped.
LARGE_JSON_THRESHOLD = 500
LARGE_JSON_MAX_SCORE = 0.05


# ---------------------------------------------------------------------------
# Load scoring prompt from references/
# ---------------------------------------------------------------------------

def _load_scoring_prompt() -> str:
    """Read the scoring rubric shipped alongside this script."""
    prompt_path = _SCRIPT_DIR.parent / "references" / "commit_scoring_prompt.md"
    if not prompt_path.is_file():
        print(
            f"Warning: scoring prompt not found at {prompt_path}; "
            "using built-in fallback.",
            file=sys.stderr,
        )
        return _FALLBACK_SYSTEM_PROMPT
    return prompt_path.read_text(encoding="utf-8")


_FALLBACK_SYSTEM_PROMPT = (
    "You are an expert panel of 3 senior software engineers evaluating code "
    "commits for a productivity analysis. Score each file from 0.0 to 1.0 "
    "based on intellectual effort and value, NOT just lines of code."
)


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

_claude_model: str | None = None


def _get_latest_haiku_model(client: anthropic.Anthropic) -> str:
    """Auto-discover the latest Haiku model; fall back to a known ID."""
    global _claude_model
    if _claude_model:
        return _claude_model

    try:
        models = client.models.list(limit=100)
        for model in models.data:
            if "haiku" in model.id.lower():
                _claude_model = model.id
                print(f"Using model: {model.display_name} ({model.id})")
                return _claude_model
    except Exception as exc:
        print(f"Warning: could not list models ({exc}); using fallback.")

    _claude_model = "claude-haiku-4-5-20251001"
    print(f"Using fallback model: {_claude_model}")
    return _claude_model


# ---------------------------------------------------------------------------
# Consent notice
# ---------------------------------------------------------------------------

def _ensure_consent() -> None:
    """Display a one-time notice about sending diffs to the Anthropic API."""
    if _CONSENT_FILE.is_file():
        return

    print()
    print("=" * 70)
    print("DATA CLASSIFICATION NOTICE")
    print("=" * 70)
    print(
        "Commit scoring sends file diffs and commit metadata to the\n"
        "Anthropic API (Claude Haiku) for evaluation. The data is processed\n"
        "according to Anthropic's API Terms of Service and is NOT used for\n"
        "model training.\n"
        "\n"
        "What is sent:\n"
        "  - File paths and diff content (truncated to ~10 KB per file)\n"
        "  - Commit titles and messages\n"
        "  - Line-change counts\n"
        "\n"
        "What is NOT sent:\n"
        "  - API tokens, credentials, or secrets\n"
        "  - Full file contents (only diff hunks)\n"
        "  - Developer names or emails\n"
    )
    print("=" * 70)
    print()

    _CONSENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONSENT_FILE.write_text(
        datetime.now(timezone.utc).isoformat(), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Auto-scoring helpers
# ---------------------------------------------------------------------------

def _auto_score_file(file_path: str) -> float | None:
    """Return a fixed score if the file matches a trivial pattern, else None."""
    for pattern, score in AUTO_SCORE_PATTERNS.items():
        if re.match(pattern, file_path, re.IGNORECASE):
            return score
    return None


def _apply_score_caps(file_path: str, score: float, lines: int) -> float:
    """Cap scores based on file type and size."""
    _, ext = os.path.splitext(file_path.lower())

    if ext in MAX_SCORE_BY_EXTENSION:
        score = min(score, MAX_SCORE_BY_EXTENSION[ext])

    if ext == ".json" and "package.json" not in file_path.lower():
        if lines > LARGE_JSON_THRESHOLD:
            score = min(score, LARGE_JSON_MAX_SCORE)

    return score


# ---------------------------------------------------------------------------
# Diff utilities
# ---------------------------------------------------------------------------

def _truncate_diff(diff_content: str | None, max_chars: int = 10_000) -> str:
    if not diff_content:
        return ""
    if len(diff_content) <= max_chars:
        return diff_content
    return diff_content[:max_chars] + "\n... [truncated]"


# ---------------------------------------------------------------------------
# Claude interaction
# ---------------------------------------------------------------------------

def _parse_json_response(text: str) -> dict | None:
    """Extract a JSON object from Claude's response."""
    clean = text.strip()

    # Strip markdown code fences.
    if "```" in clean:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean)
        if m:
            clean = m.group(1)
        else:
            clean = clean.replace("```json", "").replace("```", "")
    clean = clean.strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{[\s\S]*\}", clean)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    return None


def _score_commit_with_claude(
    client: anthropic.Anthropic,
    system_prompt: str,
    commit: dict,
    diffs: list[dict],
    max_retries: int = 3,
) -> dict | None:
    """Call Claude to score every file in a commit."""

    file_list_lines = []
    for d in diffs:
        adds = d.get("additions") or 0
        dels = d.get("deletions") or 0
        file_list_lines.append(f"- {d['file_path']} (+{adds}/-{dels})")

    def _build_prompt(diff_max: int) -> str:
        parts = []
        for d in diffs:
            fp = d["file_path"]
            auto = _auto_score_file(fp)
            if auto is not None:
                parts.append(f"### {fp}\n[Auto-scored: {auto} - lockfile/generated]")
            else:
                trunc = _truncate_diff(d.get("diff"), diff_max)
                parts.append(f"### {fp}\n```diff\n{trunc}\n```")

        return (
            "Analyze this commit and score EACH FILE separately.\n\n"
            f"**Commit Message:**\n{commit.get('title', '')}\n\n"
            f"**Full Message:**\n{(commit.get('message') or commit.get('title', ''))[:500]}\n\n"
            f"**Files Changed:**\n" + "\n".join(file_list_lines) + "\n\n"
            f"**Diffs by File:**\n" + "\n".join(parts) + "\n\n"
            "Respond with ONLY valid JSON (no markdown, no code blocks, just raw JSON):\n"
            '{\n  "files": [\n    {\n      "path": "path/to/file.js",\n'
            '      "score": 0.5,\n      "category": "feature",\n'
            '      "reasoning": "brief explanation"\n    }\n  ],\n'
            '  "overall_category": "feature",\n  "flags": []\n}'
        )

    diff_sizes = [10_000, 2_000, 1_000]
    last_error: str | None = None

    for attempt in range(max_retries):
        diff_max = diff_sizes[min(attempt, len(diff_sizes) - 1)]
        user_prompt = _build_prompt(diff_max)

        try:
            model = _get_latest_haiku_model(client)
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            result = _parse_json_response(response.content[0].text)
            if result and "files" in result:
                return result
            last_error = "Invalid JSON structure (missing 'files' key)"

        except anthropic.RateLimitError:
            wait = (2 ** attempt) * 2
            time.sleep(wait)
            last_error = "Rate limit"
            continue

        except anthropic.APIError as exc:
            last_error = f"API error: {exc}"

        except Exception as exc:
            last_error = f"Unexpected error: {exc}"

        if attempt < max_retries - 1:
            time.sleep(0.5)

    print(f"  Error after {max_retries} attempts: {last_error}")
    return None


# ---------------------------------------------------------------------------
# Productivity calculation
# ---------------------------------------------------------------------------

def _calculate_productivity(score_result: dict, diffs: list[dict]) -> dict | None:
    """Aggregate per-file scores into commit-level productivity metrics."""
    if not score_result or "files" not in score_result:
        return None

    diff_lines: dict[str, dict] = {}
    for d in diffs:
        adds = d.get("additions") or 0
        dels = d.get("deletions") or 0
        diff_lines[d["file_path"]] = {
            "additions": adds,
            "deletions": dels,
            "total": adds + dels,
        }

    total_productivity = 0.0
    total_lines = 0
    file_scores: list[dict] = []

    for fs in score_result["files"]:
        path = fs["path"]
        if path not in diff_lines:
            continue

        info = diff_lines[path]
        lines = info["total"]
        score = _apply_score_caps(path, fs["score"], lines)

        # productivity = score * max(additions, 1)
        productivity = score * max(info["additions"], 1)
        total_productivity += productivity
        total_lines += lines

        cat = fs.get("category", "unknown")
        if cat not in VALID_CATEGORIES:
            cat = "chore"

        file_scores.append({
            "commit_sha": "",  # filled in by caller
            "file_path": path,
            "score": round(score, 4),
            "category": cat,
            "reasoning": (fs.get("reasoning") or "")[:1000],
            "scored_at": "",  # filled in by caller
        })

    weighted_score = total_productivity / total_lines if total_lines > 0 else 0.0

    return {
        "files": file_scores,
        "total_productivity": round(total_productivity, 4),
        "weighted_score": round(weighted_score, 4),
        "total_lines": total_lines,
        "overall_category": score_result.get("overall_category", "unknown"),
    }


# ---------------------------------------------------------------------------
# Data fetching from storage
# ---------------------------------------------------------------------------

def _fetch_unscored_commits(
    storage: Storage,
    excluded_devs: list[str],
) -> list[dict]:
    """Return commits that have no entry in commit_scores yet."""
    rows = storage.query(
        "SELECT c.* FROM commits c "
        "LEFT JOIN commit_scores cs ON c.commit_sha = cs.commit_sha "
        "WHERE cs.commit_sha IS NULL "
        "ORDER BY c.committed_date DESC"
    )

    if excluded_devs:
        lower_excl = {e.lower() for e in excluded_devs}
        rows = [
            r for r in rows
            if (r.get("author_email") or "").lower() not in lower_excl
            and (r.get("author_name") or "").lower() not in lower_excl
        ]

    return rows


def _fetch_diffs_for_commit(storage: Storage, sha: str) -> list[dict]:
    """Retrieve diffs for a single commit."""
    return storage.query(
        f"SELECT * FROM diffs WHERE commit_sha = '{sha}'"
    )


# ---------------------------------------------------------------------------
# Progress display
# ---------------------------------------------------------------------------

_progress_lock = threading.Lock()
_start_time: float = 0.0


def _print_progress(done: int, total: int) -> None:
    if total == 0:
        return
    pct = done * 100 // total
    elapsed = time.time() - _start_time
    if done > 0:
        remaining = elapsed / done * (total - done)
        mins = int(remaining // 60)
        label = f"~{mins} min remaining" if mins > 0 else "<1 min remaining"
    else:
        label = "estimating..."
    print(
        f"\rScoring commits... {done:,}/{total:,} ({pct}%) | {label}   ",
        end="",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def score_commits(
    config: Config,
    workers: int = 10,
    batch_size: int = 50,
) -> list[dict]:
    """Score all unscored commits and persist results."""

    global _start_time

    _ensure_consent()

    # Load scoring prompt
    system_prompt = _load_scoring_prompt()

    # Anthropic client
    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        print("Error: ANTHROPIC_API_KEY is not set in config.", file=sys.stderr)
        sys.exit(1)
    claude = anthropic.Anthropic(api_key=api_key)

    # Discover model
    _get_latest_haiku_model(claude)

    # Storage
    storage = get_storage(config)
    storage.create_tables()

    # Commits to score
    excluded = config.EXCLUDED_DEVELOPERS
    commits = _fetch_unscored_commits(storage, excluded)

    if not commits:
        print("All commits are already scored. Nothing to do.")
        storage.close()
        return []

    print(f"Found {len(commits):,} unscored commits.")
    print(f"Using {workers} parallel workers, batch size {batch_size}.")

    # Pre-fetch all diffs from storage BEFORE starting the thread pool.
    # DuckDB connections are not thread-safe, so all DB access must happen
    # on the main thread.  Workers only make Claude API calls.
    print("Pre-fetching diffs for all commits...", flush=True)
    commit_diffs: dict[str, list[dict]] = {}
    for i, c in enumerate(commits):
        sha = c["commit_sha"]
        commit_diffs[sha] = _fetch_diffs_for_commit(storage, sha)
        if (i + 1) % 200 == 0:
            print(f"  Pre-fetched diffs for {i + 1}/{len(commits)} commits", flush=True)
    print(f"  Pre-fetched diffs for all {len(commits)} commits", flush=True)

    results: list[dict] = []
    batch_commit_rows: list[dict] = []
    batch_file_rows: list[dict] = []
    done = 0
    lock = threading.Lock()
    _start_time = time.time()

    def _process(commit: dict) -> dict | None:
        sha = commit["commit_sha"]
        diffs = commit_diffs.get(sha, [])
        if not diffs:
            return None

        score_result = _score_commit_with_claude(claude, system_prompt, commit, diffs)
        if not score_result:
            return None

        prod = _calculate_productivity(score_result, diffs)
        if not prod:
            return None

        scored_at = datetime.now(timezone.utc).isoformat()

        # Fill in commit_sha and scored_at for file rows.
        for f in prod["files"]:
            f["commit_sha"] = sha
            f["scored_at"] = scored_at

        return {
            "commit_sha": sha,
            "total_productivity": prod["total_productivity"],
            "weighted_score": prod["weighted_score"],
            "overall_category": prod["overall_category"],
            "scored_at": scored_at,
            "files": prod["files"],
            "total_lines": prod["total_lines"],
        }

    def _flush_batch() -> None:
        """Write accumulated rows to storage."""
        nonlocal batch_commit_rows, batch_file_rows
        if batch_commit_rows:
            storage.insert_batch("commit_scores", batch_commit_rows)
        if batch_file_rows:
            storage.insert_batch("file_scores", batch_file_rows)
        batch_commit_rows.clear()
        batch_file_rows.clear()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process, c): c for c in commits}

        try:
            for future in as_completed(futures):
                result = future.result()
                done += 1

                with lock:
                    if result:
                        results.append(result)

                        commit_row = {
                            k: result[k]
                            for k in ("commit_sha", "total_productivity",
                                      "weighted_score", "overall_category",
                                      "scored_at")
                        }
                        batch_commit_rows.append(commit_row)
                        batch_file_rows.extend(result["files"])

                        if len(batch_commit_rows) >= batch_size:
                            _flush_batch()

                with _progress_lock:
                    _print_progress(done, len(commits))
        finally:
            # Ensure partial results are flushed and storage is closed
            # even if a worker raises an exception.
            _flush_batch()
            storage.close()

    print()  # newline after progress
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_summary(results: list[dict]) -> None:
    if not results:
        print("No commits were scored.")
        return

    total = len(results)
    avg_score = sum(r["weighted_score"] for r in results) / total
    total_prod = sum(r["total_productivity"] for r in results)
    total_lines = sum(r["total_lines"] for r in results)

    # Category breakdown
    cat_counts: dict[str, int] = {}
    for r in results:
        cat = r["overall_category"]
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    print()
    print("=" * 60)
    print("SCORING SUMMARY")
    print("=" * 60)
    print(f"  Commits scored:      {total:,}")
    print(f"  Average score:       {avg_score:.2f}")
    print(f"  Total productivity:  {total_prod:,.1f}")
    print(f"  Total lines:         {total_lines:,}")
    print()
    print("  Category breakdown:")
    for cat, count in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"    {cat:<16} {count:>5}  ({count*100//total}%)")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score commits using Claude Haiku."
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to .coding-productivity.env (auto-detected if omitted).",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=10,
        help="Number of parallel API workers (default: 10).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Write to storage every N scored commits (default: 50).",
    )

    args = parser.parse_args()

    config = Config(args.config) if args.config else Config()
    errors = config.validate()
    if errors:
        for e in errors:
            print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    if not config.SCORING_ENABLED:
        print(
            "Scoring is disabled. Set SCORING_ENABLED=true in your config.",
            file=sys.stderr,
        )
        sys.exit(1)

    results = score_commits(config, workers=args.workers, batch_size=args.batch_size)
    _print_summary(results)


if __name__ == "__main__":
    main()
