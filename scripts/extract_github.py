#!/usr/bin/env python3
"""
GitHub extraction script for the coding-productivity plugin.

Extracts commits, diffs, pull requests, and PR-commit links from one or more
GitHub repositories and writes them to the configured storage backend
(DuckDB or BigQuery).

Usage::

    python extract_github.py --config /path/to/.coding-productivity.env \\
        --since 2025-01-01 --until 2025-12-31

    # Single repo
    python extract_github.py --config .coding-productivity.env \\
        --repo owner/repo --since 2025-01-01

"""

from __future__ import annotations

import argparse
import sys
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
from lib.github_client import GitHubClient
from lib import checkpoint
from lib import anonymize
from lib import bots

# ---------------------------------------------------------------------------
# Identity columns that must be pseudonymized when anonymization is enabled.
# ---------------------------------------------------------------------------
_IDENTITY_COLUMNS = [
    "author_name",
    "author_email",
    "committer_name",
    "committer_email",
]

_MAPPING_FILENAME = "identity_mapping.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _progress(repo: str, noun: str, current: int, total: int) -> None:
    if total > 0:
        pct = current * 100 // total
        print(
            f"[{repo}] Extracting {noun}... "
            f"{current:,}/{total:,} ({pct}%)",
            flush=True,
        )
    else:
        print(
            f"[{repo}] Extracting {noun}... {current:,}",
            flush=True,
        )


def _calculate_hours(start: Optional[str], end: Optional[str]) -> Optional[float]:
    """Return hours between two ISO timestamps, or None."""
    if not start or not end:
        return None
    try:
        t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return (t1 - t0).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Commit extraction
# ---------------------------------------------------------------------------

def _extract_commits(
    client: GitHubClient,
    storage: Storage,
    repo: str,
    since: Optional[str],
    until: Optional[str],
    batch_size: int,
    anon_key: Optional[bytes],
    merges: Optional[dict],
    mapping_path: Optional[Path],
) -> int:
    """Extract commits for *repo* and insert into storage.  Returns count."""

    slug = repo.replace("/", "_")
    ckpt = checkpoint.load(slug)

    # Load existing SHAs for dedup.
    try:
        existing_shas = storage.get_existing_shas("commits")
    except Exception:
        existing_shas = set()

    print(f"[{repo}] {len(existing_shas):,} existing commits in storage", flush=True)
    print(f"[{repo}] Fetching commit list...", flush=True)

    # Collect commit stubs from the API.
    raw_commits: list[dict] = []
    try:
        for c in client.get_commits(repo, since=since, until=until):
            raw_commits.append(c)
    except Exception as exc:
        print(f"[{repo}] Error fetching commits: {exc}", flush=True)
        return 0

    if not raw_commits:
        print(f"[{repo}] No commits in date range", flush=True)
        return 0

    # Determine where to resume.
    resume_after: Optional[str] = None
    if ckpt and ckpt.get("phase") == "commits":
        resume_after = ckpt.get("last_sha")

    total = len(raw_commits)
    batch: list[dict] = []
    inserted = 0
    skipping = resume_after is not None

    for idx, commit_stub in enumerate(raw_commits, 1):
        sha = commit_stub["sha"]

        # Fast-forward past already-checkpointed commits.
        if skipping:
            if sha == resume_after:
                skipping = False
            continue

        # Dedup against storage.
        if sha in existing_shas:
            continue

        # Fetch full commit detail (includes file patches and stats).
        detail = client.get_commit_detail(repo, sha)
        if detail is None:
            continue

        git_commit = detail.get("commit", {})
        author_info = git_commit.get("author", {})
        committer_info = git_commit.get("committer", {})
        stats = detail.get("stats", {})

        author_name = author_info.get("name", "")
        author_email = author_info.get("email", "")
        committer_name = committer_info.get("name", "")
        committer_email = committer_info.get("email", "")

        # Bot filter (before anonymization so we match on real names).
        if bots.is_bot(name=author_name, email=author_email):
            continue

        authored_date = author_info.get("date")
        committed_date = committer_info.get("date")

        record: dict = {
            "commit_sha": sha,
            "project_id": str(commit_stub.get("repository", {}).get("id", "")),
            "project_name": repo.split("/")[-1],
            "project_path": repo,
            "author_name": author_name,
            "author_email": author_email,
            "committer_name": committer_name,
            "committer_email": committer_email,
            "authored_date": authored_date,
            "committed_date": committed_date,
            "created_at": authored_date,
            "branch_name": None,
            "title": git_commit.get("message", "").split("\n")[0][:255],
            "message": git_commit.get("message", "")[:5000],
            "additions": stats.get("additions", 0),
            "deletions": stats.get("deletions", 0),
            "total_changes": stats.get("total", 0),
            "parent_ids": ",".join(p["sha"] for p in detail.get("parents", [])),
            "web_url": detail.get("html_url", ""),
            "extracted_at": _now_iso(),
        }

        # Build reverse mapping *before* hashing so we preserve originals.
        if anon_key and mapping_path:
            hashed_preview = anonymize.pseudonymize(
                anonymize.resolve_merge(author_email, merges) if merges else author_email,
                anon_key,
            )
            anonymize.build_or_update_mapping(
                author_name, author_email, hashed_preview, mapping_path,
            )

        # Identity merge + pseudonymize.
        if anon_key:
            anonymize.hash_record(record, _IDENTITY_COLUMNS, anon_key, merges)

        batch.append(record)
        existing_shas.add(sha)

        # Flush batch.
        if len(batch) >= batch_size:
            storage.insert_batch("commits", batch)
            inserted += len(batch)
            _progress(repo, "commits", inserted, total)
            checkpoint.save(slug, {"phase": "commits", "last_sha": sha})
            batch = []

    # Flush remaining.
    if batch:
        storage.insert_batch("commits", batch)
        inserted += len(batch)
        _progress(repo, "commits", inserted, total)
        checkpoint.save(slug, {"phase": "commits", "last_sha": batch[-1]["commit_sha"]})

    print(f"[{repo}] Commits done: {inserted:,} new", flush=True)
    return inserted


# ---------------------------------------------------------------------------
# Diff extraction
# ---------------------------------------------------------------------------

def _extract_diffs(
    client: GitHubClient,
    storage: Storage,
    repo: str,
    since: Optional[str],
    until: Optional[str],
    batch_size: int,
) -> int:
    """Extract file-level diffs for new commits and insert into storage."""

    slug = repo.replace("/", "_")

    # Determine which SHAs already have diffs.
    try:
        existing_diff_shas = storage.get_existing_shas("diffs")
    except Exception:
        existing_diff_shas = set()

    # Get commit SHAs from the commits table that do NOT yet have diffs.
    try:
        commit_shas_in_storage = storage.get_existing_shas("commits")
    except Exception:
        commit_shas_in_storage = set()

    need_diffs = commit_shas_in_storage - existing_diff_shas
    if not need_diffs:
        print(f"[{repo}] No new diffs to extract", flush=True)
        return 0

    total = len(need_diffs)
    print(f"[{repo}] Extracting diffs for {total:,} commits...", flush=True)

    batch: list[dict] = []
    inserted = 0

    for idx, sha in enumerate(sorted(need_diffs), 1):
        detail = client.get_commit_detail(repo, sha)
        if detail is None:
            continue

        files = detail.get("files", [])
        for f in files:
            diff_content = f.get("patch", "")
            if diff_content and len(diff_content) > 50_000:
                diff_content = diff_content[:50_000] + "\n... [truncated]"

            status = f.get("status", "")
            diff_record: dict = {
                "commit_sha": sha,
                "file_path": f.get("filename", ""),
                "old_path": f.get("previous_filename", f.get("filename", "")),
                "new_path": f.get("filename", ""),
                "diff": diff_content,
                "new_file": status == "added",
                "renamed_file": status == "renamed",
                "deleted_file": status == "removed",
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "extracted_at": _now_iso(),
            }
            batch.append(diff_record)

        if len(batch) >= batch_size:
            storage.insert_batch("diffs", batch)
            inserted += len(batch)
            _progress(repo, "diffs", idx, total)
            checkpoint.save(slug, {"phase": "diffs", "last_sha": sha})
            batch = []

    if batch:
        storage.insert_batch("diffs", batch)
        inserted += len(batch)

    print(f"[{repo}] Diffs done: {inserted:,} file-level records", flush=True)
    return inserted


# ---------------------------------------------------------------------------
# Pull request extraction
# ---------------------------------------------------------------------------

def _extract_pull_requests(
    client: GitHubClient,
    storage: Storage,
    repo: str,
    since: Optional[str],
    batch_size: int,
    anon_key: Optional[bytes],
    merges: Optional[dict],
    mapping_path: Optional[Path],
) -> int:
    """Extract pull requests and insert into storage.  Returns count."""

    slug = repo.replace("/", "_")

    # Existing PR IDs for dedup.
    try:
        rows = storage.query(
            f"SELECT DISTINCT mr_iid FROM merge_requests "
            f"WHERE project_path = '{repo}'"
        )
        existing_iids = {str(r["mr_iid"]) for r in rows}
    except Exception:
        existing_iids = set()

    print(f"[{repo}] {len(existing_iids):,} existing PRs in storage", flush=True)
    print(f"[{repo}] Fetching pull requests...", flush=True)

    batch: list[dict] = []
    inserted = 0

    try:
        for pr in client.get_pull_requests(repo, state="all"):
            pr_number = pr["number"]

            # Date filter: skip PRs updated before our window.
            updated_at = pr.get("updated_at", "")
            if since and updated_at and updated_at[:10] < since:
                continue

            # Dedup.
            if str(pr_number) in existing_iids:
                continue

            merged_at = pr.get("merged_at")
            closed_at = pr.get("closed_at")
            if merged_at:
                state = "merged"
            elif pr["state"] == "closed":
                state = "closed"
            else:
                state = "opened"

            author = pr.get("user", {})

            author_name_val = author.get("login", "")
            author_email_val = ""  # not available in PR list endpoint

            mr_record: dict = {
                "mr_id": str(pr["id"]),
                "mr_iid": str(pr_number),
                "project_id": "",
                "project_name": repo.split("/")[-1],
                "title": pr.get("title", "")[:255],
                "description": (pr.get("body") or "")[:5000],
                "state": state,
                "author_name": author_name_val,
                "author_email": author_email_val,
                "created_at": pr.get("created_at"),
                "updated_at": pr.get("updated_at"),
                "merged_at": merged_at,
                "closed_at": closed_at,
                "source_branch": pr.get("head", {}).get("ref", ""),
                "target_branch": pr.get("base", {}).get("ref", ""),
                "additions": pr.get("additions", 0) or 0,
                "deletions": pr.get("deletions", 0) or 0,
                "web_url": pr.get("html_url", ""),
                "extracted_at": _now_iso(),
            }

            if anon_key:
                anonymize.hash_record(
                    mr_record,
                    ["author_name", "author_email"],
                    anon_key,
                    merges,
                )

            batch.append(mr_record)
            inserted += 1

            if len(batch) >= batch_size:
                storage.insert_batch("merge_requests", batch)
                _progress(repo, "pull requests", inserted, 0)
                checkpoint.save(slug, {"phase": "prs", "last_iid": str(pr_number)})
                batch = []

    except Exception as exc:
        print(f"[{repo}] Error fetching pull requests: {exc}", flush=True)

    if batch:
        storage.insert_batch("merge_requests", batch)

    print(f"[{repo}] Pull requests done: {inserted:,} new", flush=True)
    return inserted


# ---------------------------------------------------------------------------
# PR-commit link extraction
# ---------------------------------------------------------------------------

def _extract_pr_commit_links(
    client: GitHubClient,
    storage: Storage,
    repo: str,
    batch_size: int,
) -> int:
    """Extract the mapping between PRs and their commits."""

    slug = repo.replace("/", "_")

    # Get PR IIDs that are in storage but have no links yet.
    try:
        pr_rows = storage.query(
            f"SELECT DISTINCT mr_iid FROM merge_requests "
            f"WHERE project_path = '{repo}'"
        )
    except Exception:
        pr_rows = []

    try:
        linked_rows = storage.query(
            f"SELECT DISTINCT mr_iid FROM mr_commits "
            f"WHERE project_id = '{repo}'"
        )
        linked_iids = {str(r["mr_iid"]) for r in linked_rows}
    except Exception:
        linked_iids = set()

    need_links = [r for r in pr_rows if str(r["mr_iid"]) not in linked_iids]
    if not need_links:
        print(f"[{repo}] No new PR-commit links to extract", flush=True)
        return 0

    total = len(need_links)
    print(f"[{repo}] Extracting PR-commit links for {total:,} PRs...", flush=True)

    batch: list[dict] = []
    inserted = 0

    for idx, row in enumerate(need_links, 1):
        iid = int(row["mr_iid"])
        try:
            pr_commits = client.get_pr_commits(repo, iid)
        except Exception as exc:
            print(f"[{repo}] Error fetching commits for PR #{iid}: {exc}", flush=True)
            continue

        # Find the mr_id for this iid.
        try:
            id_rows = storage.query(
                f"SELECT mr_id FROM merge_requests "
                f"WHERE mr_iid = '{iid}' AND project_path = '{repo}' LIMIT 1"
            )
            mr_id = id_rows[0]["mr_id"] if id_rows else ""
        except Exception:
            mr_id = ""

        for pc in pr_commits:
            link_record: dict = {
                "mr_id": mr_id,
                "mr_iid": str(iid),
                "commit_sha": pc["sha"],
                "project_id": repo,
                "extracted_at": _now_iso(),
            }
            batch.append(link_record)

        if len(batch) >= batch_size:
            storage.insert_batch("mr_commits", batch)
            inserted += len(batch)
            _progress(repo, "PR-commit links", idx, total)
            checkpoint.save(slug, {"phase": "pr_commits", "last_iid": str(iid)})
            batch = []

    if batch:
        storage.insert_batch("mr_commits", batch)
        inserted += len(batch)

    print(f"[{repo}] PR-commit links done: {inserted:,} records", flush=True)
    return inserted


# ---------------------------------------------------------------------------
# Per-repo orchestrator
# ---------------------------------------------------------------------------

def _process_repo(
    client: GitHubClient,
    storage: Storage,
    repo: str,
    since: Optional[str],
    until: Optional[str],
    batch_size: int,
    anon_key: Optional[bytes],
    merges: Optional[dict],
    mapping_path: Optional[Path],
) -> dict:
    """Run the full extraction pipeline for a single repository."""

    slug = repo.replace("/", "_")
    print(f"\n{'='*60}", flush=True)
    print(f"[{repo}] Starting extraction", flush=True)
    print(f"{'='*60}", flush=True)

    stats: dict = {"commits": 0, "diffs": 0, "prs": 0, "pr_commits": 0}

    try:
        # 1. Commits
        stats["commits"] = _extract_commits(
            client, storage, repo,
            since=since, until=until,
            batch_size=batch_size,
            anon_key=anon_key, merges=merges,
            mapping_path=mapping_path,
        )

        # 2. Diffs
        stats["diffs"] = _extract_diffs(
            client, storage, repo,
            since=since, until=until,
            batch_size=batch_size,
        )

        # 3. Pull requests
        stats["prs"] = _extract_pull_requests(
            client, storage, repo,
            since=since,
            batch_size=batch_size,
            anon_key=anon_key, merges=merges,
            mapping_path=mapping_path,
        )

        # 4. PR-commit links
        stats["pr_commits"] = _extract_pr_commit_links(
            client, storage, repo,
            batch_size=batch_size,
        )

        # All phases complete -- clear checkpoint.
        checkpoint.clear(slug)

    except Exception as exc:
        print(f"[{repo}] Fatal error: {exc}", flush=True)
        # Checkpoint is preserved so we can resume.

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract GitHub data into coding-productivity storage.",
    )
    parser.add_argument(
        "--config", required=True,
        help="Path to .coding-productivity.env config file.",
    )
    parser.add_argument(
        "--repo",
        help="Extract a single repo (owner/name).  Overrides REPOS in config.",
    )
    parser.add_argument(
        "--since",
        help="Start date for extraction (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--until",
        help="End date for extraction (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=500,
        help="Records per storage batch (default: 500).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # ── Config ────────────────────────────────────────────────────────────
    cfg = Config(args.config)
    errors = cfg.validate()
    if errors:
        for err in errors:
            print(f"Config error: {err}", file=sys.stderr)
        sys.exit(1)

    token = cfg.GITHUB_TOKEN
    if not token:
        print("Error: GITHUB_TOKEN not set in config.", file=sys.stderr)
        sys.exit(1)

    # ── Repos ─────────────────────────────────────────────────────────────
    if args.repo:
        repos = [args.repo]
    else:
        repos = cfg.REPOS
        if not repos:
            print("Error: No repos configured (set REPOS in config or use --repo).",
                  file=sys.stderr)
            sys.exit(1)

    # ── Storage ───────────────────────────────────────────────────────────
    storage = get_storage(cfg)
    storage.create_tables()

    # ── Bot overrides ─────────────────────────────────────────────────────
    if cfg.BOT_OVERRIDES:
        bots.load_overrides(cfg.BOT_OVERRIDES)

    # ── Anonymization ─────────────────────────────────────────────────────
    anon_key: Optional[bytes] = None
    merges: Optional[dict] = None
    mapping_path: Optional[Path] = None

    if cfg.ANONYMIZATION_ENABLED:
        raw_key = cfg.PSEUDONYMIZATION_KEY
        if raw_key:
            anon_key = anonymize.load_key(raw_key.hex())
            merges = cfg.IDENTITY_MERGES or None
            mapping_path = Path(cfg._path).parent / ".coding-productivity" / _MAPPING_FILENAME
        else:
            print("Warning: ANONYMIZATION_ENABLED but no PSEUDONYMIZATION_KEY set.",
                  flush=True)

    # ── Client ────────────────────────────────────────────────────────────
    client = GitHubClient(token)

    # ── Run ────────────────────────────────────────────────────────────────
    print(f"GitHub extraction starting", flush=True)
    print(f"  Repos:       {', '.join(repos)}", flush=True)
    print(f"  Since:       {args.since or '(all)'}", flush=True)
    print(f"  Until:       {args.until or '(all)'}", flush=True)
    print(f"  Batch size:  {args.batch_size:,}", flush=True)
    print(f"  Anonymize:   {cfg.ANONYMIZATION_ENABLED}", flush=True)
    print(f"  Storage:     {cfg.STORAGE_BACKEND}", flush=True)

    totals: dict = {"commits": 0, "diffs": 0, "prs": 0, "pr_commits": 0}

    with storage:
        for repo_slug in repos:
            stats = _process_repo(
                client, storage, repo_slug,
                since=args.since, until=args.until,
                batch_size=args.batch_size,
                anon_key=anon_key, merges=merges,
                mapping_path=mapping_path,
            )
            for k in totals:
                totals[k] += stats.get(k, 0)

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'='*60}", flush=True)
    print(f"Extraction complete", flush=True)
    print(f"  Commits:          {totals['commits']:,}", flush=True)
    print(f"  Diffs:            {totals['diffs']:,}", flush=True)
    print(f"  Pull requests:    {totals['prs']:,}", flush=True)
    print(f"  PR-commit links:  {totals['pr_commits']:,}", flush=True)
    print(f"  API requests:     {client.request_count:,}", flush=True)
    print(f"{'='*60}", flush=True)


if __name__ == "__main__":
    main()
