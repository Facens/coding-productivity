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
import re
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
from lib import bots, merge_requests

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
    except Exception as exc:
        print(f"[{repo}] Warning: could not load existing SHAs ({exc}), dedup disabled", flush=True)
        existing_shas = set()

    print(f"[{repo}] {len(existing_shas):,} existing commits in storage", flush=True)

    # Fetch all branches and extract commits across all of them (matching
    # the multi-branch extraction pattern from the original pipeline).
    print(f"[{repo}] Fetching branches...", flush=True)
    try:
        branches = client.get_branches(repo)
    except Exception:
        branches = ["main"]  # fallback
    print(f"[{repo}] {len(branches)} branches found", flush=True)

    # Collect commit stubs from all branches, deduplicating by SHA across branches.
    raw_commits: list[dict] = []
    seen_shas: set[str] = set()
    for branch in branches:
        try:
            for c in client.get_commits(repo, since=since, until=until, sha=branch):
                if c["sha"] not in seen_shas:
                    seen_shas.add(c["sha"])
                    c["_branch"] = branch  # track source branch
                    raw_commits.append(c)
        except Exception as exc:
            print(f"[{repo}] Error on branch {branch}: {exc}", flush=True)
            continue

    if not raw_commits:
        print(f"[{repo}] No commits in date range", flush=True)
        return 0
    print(f"[{repo}] {len(raw_commits)} unique commits across {len(branches)} branches", flush=True)

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
            "branch_name": commit_stub.get("_branch"),
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

    # Get commit SHAs for THIS repo that do NOT yet have diffs.
    try:
        rows = storage.query(
            "SELECT commit_sha FROM commits WHERE project_path = $repo "
            "AND commit_sha NOT IN (SELECT commit_sha FROM diffs)",
            {"repo": repo},
        )
        need_diffs = {r["commit_sha"] for r in rows}
    except Exception:
        need_diffs = set()
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

def _pr_url_pattern(repo: str) -> str:
    """Coarse LIKE pattern for the web_url of this repo's PRs (``owner/name`` slug).

    ``_`` is a LIKE wildcard and the two backends escape differently, so it can
    also match a same-owner repo (``my_repo`` vs ``my-repo``): narrow the rows
    with ``_is_repo_pr``.
    """
    return f"%/{repo.lower()}/pull/%"


def _is_repo_pr(web_url: Optional[str], repo: str) -> bool:
    """Is *web_url* a PR of exactly *repo*?"""
    m = re.match(r"^https?://[^/]+/(.+?)/pull/\d+", web_url or "")
    return bool(m) and m.group(1).lower() == repo.lower()


def _extract_pull_requests(
    client: GitHubClient,
    storage: Storage,
    repo: str,
    since: Optional[str],
    batch_size: int,
    anon_key: Optional[bytes],
    merges: Optional[dict],
    mapping_path: Optional[Path],
    refreshed: Optional[set] = None,
) -> int:
    """Extract pull requests and insert into storage.  Returns count.

    The iids of the PRs stored in this run are added to *refreshed*.
    """

    slug = repo.replace("/", "_")

    # Latest stored version of every PR of this repo, by PR id. A PR is stored
    # again only when it changed (or gained its merge sha): see needs_refresh.
    # Scoped by URL, not short name: same-named repos of two owners share a name.
    stored = {
        mr_id: row
        for mr_id, row in merge_requests.stored_mrs(
            storage, "LOWER(m.web_url) LIKE $url_pat", {"url_pat": _pr_url_pattern(repo)}
        ).items()
        if _is_repo_pr(row["web_url"], repo)
    }

    print(f"[{repo}] {len(stored):,} existing PRs in storage", flush=True)
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
                # An open PR's merge_commit_sha is a throwaway test merge.
                "merge_commit_sha": (pr.get("merge_commit_sha") or None) if merged_at else None,
                "squash_commit_sha": None,  # GitHub has one field for every merge method
                "additions": pr.get("additions", 0) or 0,
                "deletions": pr.get("deletions", 0) or 0,
                "web_url": pr.get("html_url", ""),
                "extracted_at": _now_iso(),
            }

            if not merge_requests.needs_refresh(stored.get(mr_record["mr_id"]), mr_record):
                continue

            if anon_key:
                anonymize.hash_record(
                    mr_record,
                    ["author_name", "author_email"],
                    anon_key,
                    merges,
                )

            batch.append(mr_record)
            inserted += 1
            if refreshed is not None:
                refreshed.add(str(pr_number))

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
    refreshed: Optional[set] = None,
) -> int:
    """Extract the mapping between PRs and their commits.

    Links are fetched for PRs with none yet and for those in *refreshed* (stored
    again this run, so their commit list may have grown).
    """

    slug = repo.replace("/", "_")

    # Latest version of each PR of this repo (one row per PR, whatever
    # re-extraction left). Scoped by URL: merge_requests stores the short name,
    # which two owners' repos can share.
    pr_rows = [
        r for r in storage.query(
            f"SELECT m.mr_iid AS mr_iid, m.mr_id AS mr_id, m.web_url AS web_url "
            f"FROM {merge_requests.LATEST_MRS} m WHERE LOWER(m.web_url) LIKE $url_pat",
            {"url_pat": _pr_url_pattern(repo)},
        )
        if _is_repo_pr(r["web_url"], repo)
    ]

    linked_iids = {
        str(r["mr_iid"]) for r in storage.query(
            "SELECT DISTINCT mr_iid FROM mr_commits WHERE project_id = $repo_slug",
            {"repo_slug": repo},
        )
    }
    refreshed = refreshed or set()
    # Commit-level dedupe is only needed for PRs fetched again: read the stored
    # pairs only then, so a run with nothing refreshed stays one row per PR.
    linked: set[tuple[str, str]] = set()
    if refreshed & linked_iids:
        linked = {
            (str(r["mr_iid"]), r["commit_sha"])
            for r in storage.query(
                "SELECT mr_iid, commit_sha FROM mr_commits WHERE project_id = $repo_slug",
                {"repo_slug": repo},
            )
            if str(r["mr_iid"]) in refreshed
        }
    need_links = [
        r for r in pr_rows
        if str(r["mr_iid"]) not in linked_iids or str(r["mr_iid"]) in refreshed
    ]
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

        mr_id = row["mr_id"] or ""

        iid_s = str(row["mr_iid"])
        for pc in pr_commits:
            if (iid_s, pc["sha"]) in linked:
                continue
            linked.add((iid_s, pc["sha"]))
            link_record: dict = {
                "mr_id": mr_id,
                "mr_iid": iid_s,
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
    refreshed: set[str] = set()

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
            refreshed=refreshed,
        )

        # 4. PR-commit links
        stats["pr_commits"] = _extract_pr_commit_links(
            client, storage, repo,
            batch_size=batch_size,
            refreshed=refreshed,
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
        help="Start date for extraction (YYYY-MM-DD).  "
             "Defaults to 1 day before the latest commit in storage (incremental).",
    )
    parser.add_argument(
        "--until",
        help="End date for extraction (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Force full extraction (ignore existing data, fetch all history).",
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

    # ── Auto-since (incremental by default) ─────────────────────────────
    if not args.since and not args.full:
        try:
            rows = storage.query(
                "SELECT MAX(committed_date) AS latest FROM commits"
            )
            latest = rows[0].get("latest") if rows else None
            if latest:
                # Use the exact timestamp of the latest commit — SHA dedup
                # handles any overlap, so no buffer needed.
                dt = datetime.fromisoformat(str(latest).replace("Z", "+00:00"))
                args.since = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                print(f"  Auto-since:  {args.since} (latest commit in storage)", flush=True)
        except Exception:
            pass  # empty DB or table missing — will do full extraction

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

    try:
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
    except KeyboardInterrupt:
        print("\nExtraction interrupted by user.", flush=True)
        sys.exit(1)

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
