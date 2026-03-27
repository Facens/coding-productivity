#!/usr/bin/env python3
"""
GitLab extraction script for the coding-productivity plugin.

Extracts commits (with diffs) and merge requests from GitLab projects,
applies bot filtering, identity-merge resolution, and HMAC pseudonymization,
then stores results via the shared storage abstraction (DuckDB or BigQuery).

Supports all branches with cross-branch SHA deduplication and checkpoint-based
resume for long-running extractions.

Usage:
    python3 extract_gitlab.py --config .coding-productivity.env \\
        --since 2025-01-01 --until 2025-12-31

    python3 extract_gitlab.py --config .coding-productivity.env \\
        --project 14891 --since 2025-01-01 --until 2025-12-31

    python3 extract_gitlab.py --config .coding-productivity.env \\
        --since 2025-01-01 --until 2025-12-31 --batch-size 250
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as urlquote

# Ensure the lib package is importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import Config
from lib.storage import get_storage
from lib.gitlab_client import GitLabClient
from lib import bots, checkpoint, anonymize


# ── Helpers ───────────────────────────────────────────────────────────────────

_IDENTITY_COLS_COMMIT = ["author_name", "author_email", "committer_name", "committer_email"]
_IDENTITY_COLS_MR = ["author_name", "author_email"]
_MAPPING_PATH = Path(".coding-productivity") / "identity_mapping.json"


def _parse_date(date_str: str | None) -> str | None:
    """Normalise a GitLab ISO-8601 timestamp for storage."""
    if not date_str:
        return None
    return date_str.replace("Z", "+00:00") if date_str.endswith("Z") else date_str


def _count_diff_lines(diff_text: str) -> tuple[int, int]:
    """Return (additions, deletions) by counting +/- lines in a unified diff."""
    adds = dels = 0
    if not diff_text:
        return adds, dels
    for line in diff_text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return adds, dels


def _resolve_project_id(client: GitLabClient, identifier: str) -> dict | None:
    """Fetch project metadata by numeric ID or ``group/project`` path.

    Returns a dict with id, name, path_with_namespace, default_branch,
    or ``None`` on failure.
    """
    # Numeric IDs can be used directly; paths need URL-encoding.
    if identifier.isdigit():
        endpoint = f"/projects/{identifier}"
    else:
        encoded = urlquote(identifier, safe="")
        endpoint = f"/projects/{encoded}"

    resp = client._request(endpoint)
    if resp is None:
        return None
    data = resp.json()
    return {
        "id": data["id"],
        "name": data["name"],
        "path_with_namespace": data["path_with_namespace"],
        "default_branch": data.get("default_branch", "main"),
    }


# ── Commit extraction ────────────────────────────────────────────────────────

def _extract_commits(
    client: GitLabClient,
    storage,
    project: dict,
    *,
    since: str,
    until: str,
    batch_size: int,
    existing_shas: set[str],
    anon_key: bytes | None,
    merges: dict | None,
    excluded: set[str],
):
    """Extract commits and diffs for a single project across all active branches.

    Returns (commits_stored, diffs_stored, commits_skipped_dedup).
    """
    project_id = project["id"]
    project_name = project["name"]
    project_path = project["path_with_namespace"]
    extracted_at = datetime.now(timezone.utc).isoformat()

    # 1. List branches, filter by activity -----------------------------------
    all_branches = client.get_branches(project_id)
    active_branches: list[str] = []
    for branch in all_branches:
        commit_info = branch.get("commit", {})
        committed_date = (commit_info.get("committed_date") or "")[:10]
        if committed_date and committed_date < since:
            continue
        active_branches.append(branch["name"])

    print(
        f"  Branches: {len(active_branches)} active / {len(all_branches)} total",
        flush=True,
    )

    # 2. Walk branches, deduplicate by SHA -----------------------------------
    seen_shas: set[str] = set()  # cross-branch dedup within this run
    commits_batch: list[dict] = []
    diffs_batch: list[dict] = []
    total_commits = 0
    total_diffs = 0
    skipped_existing = 0
    skipped_dedup = 0
    skipped_bots = 0
    skipped_no_diff = 0

    for branch_name in active_branches:
        for commit in client.get_commits(
            project_id, since=since, until=until, ref_name=branch_name
        ):
            sha = commit["id"]

            # Cross-branch dedup
            if sha in seen_shas:
                skipped_dedup += 1
                continue
            seen_shas.add(sha)

            # Storage-level dedup
            if sha in existing_shas:
                skipped_existing += 1
                continue

            # Bot filtering
            author_name = commit.get("author_name", "")
            author_email = commit.get("author_email", "")
            if bots.is_bot(name=author_name, email=author_email):
                skipped_bots += 1
                continue

            # Excluded developers
            if author_name in excluded or author_email in excluded:
                skipped_bots += 1
                continue

            # Fetch diff (also skips merge commits with no diff)
            diff_files = client.get_commit_diff(project_id, sha)
            if not diff_files:
                skipped_no_diff += 1
                continue

            # Build commit record
            stats = commit.get("stats", {})
            record = {
                "commit_sha": sha,
                "project_id": str(project_id),
                "project_name": project_name,
                "project_path": project_path,
                "author_name": commit.get("author_name"),
                "author_email": commit.get("author_email"),
                "committer_name": commit.get("committer_name"),
                "committer_email": commit.get("committer_email"),
                "authored_date": _parse_date(commit.get("authored_date")),
                "committed_date": _parse_date(commit.get("committed_date")),
                "created_at": _parse_date(commit.get("created_at")),
                "branch_name": branch_name,
                "title": (commit.get("title") or "")[:1000],
                "message": (commit.get("message") or "")[:5000],
                "additions": stats.get("additions", 0),
                "deletions": stats.get("deletions", 0),
                "total_changes": stats.get("total", 0),
                "parent_ids": ",".join(commit.get("parent_ids", [])),
                "web_url": commit.get("web_url"),
                "extracted_at": extracted_at,
            }

            # Anonymization: resolve merges then hash
            orig_name = record["author_name"] or ""
            orig_email = record["author_email"] or ""
            if anon_key is not None:
                anonymize.hash_record(record, _IDENTITY_COLS_COMMIT, anon_key, merges)
                # Store reverse mapping
                hashed_val = record["author_email"]
                anonymize.build_or_update_mapping(
                    orig_name, orig_email, hashed_val, _MAPPING_PATH,
                )

            commits_batch.append(record)

            # Build diff records
            for df in diff_files:
                diff_text = df.get("diff", "")
                adds, dels = _count_diff_lines(diff_text)
                diff_record = {
                    "commit_sha": sha,
                    "file_path": df.get("new_path") or df.get("old_path"),
                    "old_path": df.get("old_path"),
                    "new_path": df.get("new_path"),
                    "diff": diff_text[:50000],
                    "new_file": df.get("new_file", False),
                    "renamed_file": df.get("renamed_file", False),
                    "deleted_file": df.get("deleted_file", False),
                    "additions": adds,
                    "deletions": dels,
                    "extracted_at": extracted_at,
                }
                diffs_batch.append(diff_record)

            # Flush batch
            if len(commits_batch) >= batch_size:
                storage.insert_batch("commits", commits_batch)
                storage.insert_batch("diffs", diffs_batch)
                total_commits += len(commits_batch)
                total_diffs += len(diffs_batch)
                print(
                    f"    Stored {total_commits} commits, {total_diffs} diffs so far...",
                    flush=True,
                )
                commits_batch.clear()
                diffs_batch.clear()

    # Flush remaining
    if commits_batch:
        storage.insert_batch("commits", commits_batch)
        storage.insert_batch("diffs", diffs_batch)
        total_commits += len(commits_batch)
        total_diffs += len(diffs_batch)

    # Summary
    skip_parts: list[str] = []
    if skipped_existing:
        skip_parts.append(f"{skipped_existing} already stored")
    if skipped_dedup:
        skip_parts.append(f"{skipped_dedup} cross-branch dupes")
    if skipped_bots:
        skip_parts.append(f"{skipped_bots} bots/excluded")
    if skipped_no_diff:
        skip_parts.append(f"{skipped_no_diff} no diffs")
    skip_msg = f" (skipped: {', '.join(skip_parts)})" if skip_parts else ""
    print(
        f"  Commits: {total_commits} stored, Diffs: {total_diffs} stored{skip_msg}",
        flush=True,
    )

    return total_commits, total_diffs, skipped_dedup


# ── Merge request extraction ─────────────────────────────────────────────────

def _extract_merge_requests(
    client: GitLabClient,
    storage,
    project: dict,
    *,
    since: str,
    until: str,
    batch_size: int,
    anon_key: bytes | None,
    merges: dict | None,
    excluded: set[str],
):
    """Extract merge requests and MR-commit links for a single project.

    Returns (mrs_stored, mr_commits_stored).
    """
    project_id = project["id"]
    project_name = project["name"]
    extracted_at = datetime.now(timezone.utc).isoformat()

    mrs_batch: list[dict] = []
    mr_commits_batch: list[dict] = []
    total_mrs = 0
    total_mr_commits = 0
    skipped_bots = 0

    for mr in client.get_merge_requests(project_id, state="all"):
        # Date-range filtering on updated_at
        updated_at = mr.get("updated_at", "")
        if updated_at:
            update_date = updated_at[:10]
            if update_date < since or update_date > until:
                continue

        # Bot filtering on author
        author_info = mr.get("author") or {}
        author_name = author_info.get("name", "")
        author_email = author_info.get("email", "")
        if bots.is_bot(name=author_name, email=author_email):
            skipped_bots += 1
            continue
        if author_name in excluded:
            skipped_bots += 1
            continue

        # Build MR record matching the schema
        changes = mr.get("changes", {})
        mr_record = {
            "mr_id": str(mr["id"]),
            "mr_iid": str(mr["iid"]),
            "project_id": str(project_id),
            "project_name": project_name,
            "title": (mr.get("title") or "")[:1000],
            "description": (mr.get("description") or "")[:5000],
            "state": mr.get("state"),
            "author_name": author_name,
            "author_email": author_email,
            "created_at": _parse_date(mr.get("created_at")),
            "updated_at": _parse_date(mr.get("updated_at")),
            "merged_at": _parse_date(mr.get("merged_at")),
            "closed_at": _parse_date(mr.get("closed_at")),
            "source_branch": mr.get("source_branch"),
            "target_branch": mr.get("target_branch"),
            "additions": mr.get("additions"),
            "deletions": mr.get("deletions"),
            "web_url": mr.get("web_url"),
            "extracted_at": extracted_at,
        }

        # Anonymization
        orig_name = mr_record["author_name"] or ""
        orig_email = mr_record["author_email"] or ""
        if anon_key is not None:
            anonymize.hash_record(mr_record, _IDENTITY_COLS_MR, anon_key, merges)
            hashed_val = mr_record["author_email"]
            anonymize.build_or_update_mapping(
                orig_name, orig_email, hashed_val, _MAPPING_PATH,
            )

        mrs_batch.append(mr_record)

        # Fetch commits for this MR
        mr_commits = client.get_mr_commits(project_id, mr["iid"])
        for c in mr_commits:
            link = {
                "mr_id": str(mr["id"]),
                "mr_iid": str(mr["iid"]),
                "commit_sha": c.get("id") or c.get("sha", ""),
                "project_id": str(project_id),
                "extracted_at": extracted_at,
            }
            mr_commits_batch.append(link)

        # Flush
        if len(mrs_batch) >= batch_size:
            storage.insert_batch("merge_requests", mrs_batch)
            storage.insert_batch("mr_commits", mr_commits_batch)
            total_mrs += len(mrs_batch)
            total_mr_commits += len(mr_commits_batch)
            print(
                f"    Stored {total_mrs} MRs, {total_mr_commits} MR-commit links so far...",
                flush=True,
            )
            mrs_batch.clear()
            mr_commits_batch.clear()

    # Flush remaining
    if mrs_batch:
        storage.insert_batch("merge_requests", mrs_batch)
        storage.insert_batch("mr_commits", mr_commits_batch)
        total_mrs += len(mrs_batch)
        total_mr_commits += len(mr_commits_batch)

    skip_msg = f" (skipped {skipped_bots} bots/excluded)" if skipped_bots else ""
    print(
        f"  MRs: {total_mrs} stored, MR-commits: {total_mr_commits} stored{skip_msg}",
        flush=True,
    )
    return total_mrs, total_mr_commits


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(
    config: Config,
    *,
    single_project: str | None = None,
    since: str,
    until: str,
    batch_size: int = 500,
):
    """Execute the full GitLab extraction pipeline."""

    # ── Validate config ──────────────────────────────────────────────────
    token = config.GITLAB_TOKEN
    if not token:
        print("Error: GITLAB_TOKEN is not set in the config file.", flush=True)
        sys.exit(1)

    base_url = config.GITLAB_URL or "https://gitlab.com"

    # ── Initialise clients ───────────────────────────────────────────────
    client = GitLabClient(
        token=token,
        base_url=base_url,
        ca_bundle=config.GITLAB_CA_BUNDLE,
    )
    storage = get_storage(config)

    print("=" * 60, flush=True)
    print("GitLab Extraction", flush=True)
    print("=" * 60, flush=True)
    print(f"  Instance : {base_url}", flush=True)
    print(f"  Since    : {since}", flush=True)
    print(f"  Until    : {until}", flush=True)
    print(f"  Batch    : {batch_size}", flush=True)

    # ── Bot overrides ────────────────────────────────────────────────────
    bots.load_overrides(config.BOT_OVERRIDES)
    excluded_devs = set(config.EXCLUDED_DEVELOPERS)

    # ── Anonymization setup ──────────────────────────────────────────────
    anon_key: bytes | None = None
    merges: dict | None = None

    if config.ANONYMIZATION_ENABLED:
        key_material = config.PSEUDONYMIZATION_KEY
        if not key_material:
            print("Error: PSEUDONYMIZATION_KEY required when anonymization is enabled.", flush=True)
            sys.exit(1)
        anon_key = key_material if isinstance(key_material, bytes) else key_material.encode()
        merges = config.IDENTITY_MERGES or {}
        print("  Anonymization: enabled", flush=True)
    else:
        print("  Anonymization: disabled", flush=True)

    # ── Create tables ────────────────────────────────────────────────────
    storage.create_tables()

    # ── Get existing SHAs for dedup ──────────────────────────────────────
    existing_shas = storage.get_existing_shas("commits")
    print(f"\n  Existing commits in storage: {len(existing_shas)}", flush=True)

    # ── Resolve projects ─────────────────────────────────────────────────
    projects: list[dict] = []

    if single_project:
        p = _resolve_project_id(client, single_project)
        if p is None:
            print(f"Error: could not resolve project '{single_project}'", flush=True)
            sys.exit(1)
        projects = [p]
    else:
        repos = config.REPOS
        if not repos:
            print("Error: no REPOS configured.", flush=True)
            sys.exit(1)

        for identifier in repos:
            p = _resolve_project_id(client, identifier)
            if p is None:
                print(f"  Warning: could not resolve '{identifier}', skipping.", flush=True)
                continue
            projects.append(p)

    print(f"\n  Projects to process: {len(projects)}", flush=True)

    # ── Process each project ─────────────────────────────────────────────
    grand_commits = 0
    grand_diffs = 0
    grand_mrs = 0
    grand_mr_commits = 0

    for idx, project in enumerate(projects, 1):
        project_path = project["path_with_namespace"]
        slug = project_path.replace("/", "_")

        print(
            f"\n[{idx}/{len(projects)}] {project_path}",
            flush=True,
        )

        # Checkpoint: check if already completed
        cp = checkpoint.load(slug)
        if cp and cp.get("phase") == "done":
            print("  Already completed (checkpoint). Skipping.", flush=True)
            continue

        # Phase 1: commits & diffs
        if not cp or cp.get("phase") != "mrs":
            checkpoint.save(slug, {"phase": "commits"})
            c, d, _ = _extract_commits(
                client,
                storage,
                project,
                since=since,
                until=until,
                batch_size=batch_size,
                existing_shas=existing_shas,
                anon_key=anon_key,
                merges=merges,
                excluded=excluded_devs,
            )
            grand_commits += c
            grand_diffs += d

        # Phase 2: merge requests
        checkpoint.save(slug, {"phase": "mrs"})
        m, mc = _extract_merge_requests(
            client,
            storage,
            project,
            since=since,
            until=until,
            batch_size=batch_size,
            anon_key=anon_key,
            merges=merges,
            excluded=excluded_devs,
        )
        grand_mrs += m
        grand_mr_commits += mc

        # Mark complete
        checkpoint.save(slug, {"phase": "done"})
        checkpoint.clear(slug)

    # ── Summary ──────────────────────────────────────────────────────────
    storage.close()
    print("\n" + "=" * 60, flush=True)
    print("Extraction complete", flush=True)
    print(f"  Commits          : {grand_commits}", flush=True)
    print(f"  Diffs            : {grand_diffs}", flush=True)
    print(f"  Merge requests   : {grand_mrs}", flush=True)
    print(f"  MR-commit links  : {grand_mr_commits}", flush=True)
    print(f"  API requests     : {client.request_count}", flush=True)
    print("=" * 60, flush=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract commits, diffs, and merge requests from GitLab.",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to .coding-productivity.env config file.",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Single project ID or path (e.g. 14891 or group/sub/project). "
        "Overrides REPOS in config.",
    )
    parser.add_argument(
        "--since",
        required=True,
        help="Start date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--until",
        required=True,
        help="End date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Number of records per storage batch (default: 500).",
    )
    args = parser.parse_args()

    config = Config(args.config)
    errors = config.validate()
    if errors:
        for e in errors:
            print(f"Config error: {e}", flush=True)
        sys.exit(1)

    try:
        run(
            config,
            single_project=args.project,
            since=args.since,
            until=args.until,
            batch_size=args.batch_size,
        )
    except KeyboardInterrupt:
        print("\nExtraction interrupted by user.", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
