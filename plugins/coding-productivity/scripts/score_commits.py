#!/usr/bin/env python3
"""
Score commits using Claude to measure intellectual value per file.

Reads unscored commits + diffs from the configured storage backend, sends each
file's diff to Claude for evaluation, and writes results to the *active*
scoring series (``commit_scores`` / ``file_scores``, or the versioned tables a
``rebase`` activated). Every row records the model and prompt that produced it.

The model is ``SCORING_MODEL`` from the config; it is never auto-discovered.
One series is one model. To change models, run ``calibrate`` and ``rebase``.

Usage::

    python score_commits.py --config /path/to/.coding-productivity.env
    python score_commits.py --config .coding-productivity.env --workers 5 --batch-size 25

"""

from __future__ import annotations

import argparse
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Put scripts/lib on the import path so ``from lib import ...`` works when
# the script is invoked directly.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from lib import scoring, scoring_meta
from lib import schema as _schema
from lib.config import Config
from lib.storage import get_storage, Storage


def _make_client(api_key: str):
    try:
        import anthropic
    except ImportError:
        print(
            "Error: the 'anthropic' package is required for commit scoring.\n"
            "Install it with:  pip install anthropic",
            file=sys.stderr,
        )
        sys.exit(1)
    return anthropic.Anthropic(api_key=api_key)


# ---------------------------------------------------------------------------
# Data fetching from storage
# ---------------------------------------------------------------------------

def _fetch_unscored_commits(
    storage: Storage,
    excluded_devs: list[str],
    commit_table: str = "commit_scores",
) -> list[dict]:
    """Return commits that have no entry in the active commit score table."""
    _schema.base_table(commit_table)  # whitelist check: raises on unknown names
    rows = storage.query(
        "SELECT c.* FROM commits c "
        f"LEFT JOIN {commit_table} cs ON c.commit_sha = cs.commit_sha "
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
        "SELECT * FROM diffs WHERE commit_sha = $sha", {"sha": sha}
    )


# ---------------------------------------------------------------------------
# One series = one model
# ---------------------------------------------------------------------------

def _fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _check_series(config: Config, storage: Storage,
                  active: scoring_meta.ActiveScoring,
                  settings: scoring.ScoringSettings,
                  legacy_model: str | None = None,
                  allow_prompt_change: bool = False) -> None:
    """Refuse to append scores from a different instrument to the active series.

    Instrument = model and ``prompt_version``. A model change always needs a
    rebase; a prompt_version change (prompt, effort, post-processing logic)
    needs ``--allow-prompt-change`` if the user really wants to continue the
    series, in which case analysis will refuse ranges that span both versions.
    """
    configured = config.SCORING_MODEL
    pv = settings.prompt_version
    rebase = ("run /coding-productivity:calibrate and /coding-productivity:rebase "
              "to move the whole series to the new instrument")

    def check_prompt(existing: str | None, where: str) -> None:
        if existing and existing != pv and not allow_prompt_change:
            _fail(
                f"prompt_version is {pv} but {where} was scored with {existing}: the "
                "prompt, SCORING_EFFORT or the scoring logic changed. Mixing them "
                f"inside one series breaks trends. To fix it, {rebase}. Or pass "
                "--allow-prompt-change to continue anyway (analysis will then refuse "
                "date ranges that span both versions)."
            )

    if active.from_meta:
        if active.scorer_model != configured:
            _fail(
                f"SCORING_MODEL={configured} but the active scoring series "
                f"(v{active.version}) was built with {active.scorer_model}. "
                "Mixing models inside one series breaks trends. Either set "
                f"SCORING_MODEL back to {active.scorer_model}, or {rebase}."
            )
        check_prompt(active.prompt_version, f"the active series (v{active.version})")
        return

    # Legacy series: the model is only known for rows that carry provenance.
    rows = storage.query(
        f"SELECT DISTINCT scorer_model, prompt_version FROM {active.commit_table} "
        "WHERE scorer_model IS NOT NULL"
    )
    foreign = sorted({
        r["scorer_model"] for r in rows
        if not scoring_meta.model_matches(configured, r["scorer_model"])
    })
    if foreign:
        _fail(
            f"existing scores were produced by {', '.join(foreign)} but "
            f"SCORING_MODEL={configured}. Mixing models inside one series breaks "
            f"trends. Set SCORING_MODEL to the existing model, or {rebase}."
        )
    for r in rows:
        check_prompt(r["prompt_version"], "existing rows")

    null_rows = storage.query(
        f"SELECT COUNT(*) AS n FROM {active.commit_table} WHERE scorer_model IS NULL"
    )[0]["n"]
    if null_rows:
        # Scores from before provenance existed: the model is unknown, so
        # appending labelled rows would silently splice two instruments.
        if not legacy_model:
            _fail(
                f"{null_rows:,} existing score(s) have no recorded model (written before "
                "provenance existed). Declare which model produced them with "
                f"--legacy-model MODEL (it must equal SCORING_MODEL={configured}); that is "
                "recorded as the series' model and the strict check applies from then on. "
                f"If they came from another model, {rebase}."
            )
        if legacy_model != configured and not scoring_meta.model_matches(configured, legacy_model):
            _fail(
                f"--legacy-model {legacy_model} differs from SCORING_MODEL={configured}. "
                f"Legacy scores from another model cannot be continued; {rebase}."
            )
        # Record this run's prompt_version too, so the prompt-change gate
        # applies to this series from the next run on (the pre-existing rows
        # themselves have no known prompt).
        scoring_meta.record(storage, scoring_meta.LEGACY_VERSION, "active", configured, pv)
        print(f"Recorded the legacy series (v1) as built with {configured}. Analysis over "
              "ranges that mix these rows with newly scored ones needs --allow-legacy-null.",
              flush=True)


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
    legacy_model: str | None = None,
    allow_prompt_change: bool = False,
) -> list[dict]:
    """Score all unscored commits and persist results."""

    global _start_time

    scoring.ensure_consent()

    # Anthropic client
    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        print("Error: ANTHROPIC_API_KEY is not set in config.", file=sys.stderr)
        sys.exit(1)
    claude = _make_client(api_key)

    settings = scoring.settings_or_exit(config)
    prompt_version = settings.prompt_version
    print(
        f"Scoring model: {settings.model} (prompt_version {prompt_version})",
        flush=True,
    )
    scoring.warn_if_newer_haiku(claude, settings.model)

    # Storage
    storage = get_storage(config)
    storage.create_tables()

    active = scoring_meta.get_active(storage)
    _check_series(config, storage, active, settings, legacy_model, allow_prompt_change)
    active = scoring_meta.get_active(storage)  # a declared legacy model is recorded now
    print(f"Writing to scoring series v{active.version} "
          f"({active.commit_table} / {active.file_table}).", flush=True)

    # Commits to score
    excluded = config.EXCLUDED_DEVELOPERS
    commits = _fetch_unscored_commits(storage, excluded, active.commit_table)

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
    failure_log = scoring.FailureLog()
    done = 0
    lock = threading.Lock()
    _start_time = time.time()

    def _process(commit: dict):
        sha = commit["commit_sha"]
        diffs = commit_diffs.get(sha, [])
        month = scoring.month_of(commit.get("authored_date"))
        if not diffs:
            return month, None, scoring.ScoreOutcome()

        rows, outcome = scoring.score_and_build_rows(claude, settings, commit, diffs)
        return month, rows, outcome

    def _flush_batch() -> None:
        """Write accumulated rows to storage."""
        # File rows first, commit row last: the commit row is the completion
        # marker, so a crash in between leaves the commit unscored (retried).
        if batch_file_rows:
            storage.insert_batch(active.file_table, batch_file_rows)
        if batch_commit_rows:
            storage.insert_batch(active.commit_table, batch_commit_rows)
        batch_commit_rows.clear()
        batch_file_rows.clear()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process, c): c for c in commits}

        try:
            for future in as_completed(futures):
                month, rows, outcome = future.result()
                done += 1

                with lock:
                    # Commits without diffs are skipped, not failures.
                    if rows or outcome.failures or outcome.dropped_files:
                        failure_log.record(month, rows is not None, outcome.failures,
                                           outcome.dropped_files)
                    if rows:
                        results.append({
                            **rows["commit_row"],
                            "total_lines": rows["total_lines"],
                        })
                        batch_commit_rows.append(rows["commit_row"])
                        batch_file_rows.extend(rows["file_rows"])

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
    print(failure_log.report())
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
        description="Score commits using Claude (model = SCORING_MODEL)."
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
        "--legacy-model", default=None, metavar="MODEL",
        help="Model that produced existing scores with no recorded model "
             "(must equal SCORING_MODEL). Required once to continue such a series.",
    )
    parser.add_argument(
        "--allow-prompt-change", action="store_true",
        help="Continue the active series although prompt_version changed "
             "(prompt, effort or scoring logic). Analysis will refuse ranges "
             "that span both versions.",
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

    results = score_commits(
        config, workers=args.workers, batch_size=args.batch_size,
        legacy_model=args.legacy_model, allow_prompt_change=args.allow_prompt_change,
    )
    _print_summary(results)


if __name__ == "__main__":
    main()
