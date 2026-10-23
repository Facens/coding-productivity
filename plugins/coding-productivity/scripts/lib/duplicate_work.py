"""
Duplicate-work detection: find work that the commits table counts more than once.

Five rules (see README, "Duplicate work"). Four of them exclude whole commits
(``duplicate_commits``), the fifth discounts files (``file_moved_share``):

1. merge      a commit with >= 2 parents whose second parent is in the table
2. squash     the squash/merge commit of a merged MR/PR whose own branch
              commits (merges of the base branch excluded) are in the table and
              cover >= SQUASH_COVERAGE of its lines
3. promotion  the merge/squash commit of a merged MR/PR whose SOURCE branch is
              long-lived (staging -> main, develop -> master) and some of whose
              own commits are in the table (a fork PR from `main` has none)
4. copy       same project, author, title and identical changed-line content
              per file as an earlier commit (cherry-pick, rebase, re-push)
5. moved      (file level) added lines that already appeared earlier in a
              DIFFERENT file

Everything is computed in Python on top of ``Storage`` so that DuckDB and
BigQuery behave identically and the rules can be tested offline. Memory is
bounded by the number of commits and distinct long added lines, never by the
size of the diff text: ``diffs`` is streamed twice (``Storage.iter_query``),
fingerprints are 64-bit ints, and only fingerprints seen in fewer than
MOVED_BOILERPLATE_FILES distinct files are kept.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from .storage import is_table_missing

# ── Constants: the one place the rules are tuned ─────────────────────────────
# Changing any of them changes what gets excluded: bump DEDUPE_RULES_VERSION.

DEDUPE_RULES_VERSION = 1

SQUASH_COVERAGE = 0.8          # branch commits must cover this share of a squash commit's lines
LONG_LIVED_BRANCH_RE = re.compile(
    r"^(main|master|develop|dev|development|staging|production|stage|prod)$"
    r"|^(release|support)/",
    re.IGNORECASE,
)
MOVED_MIN_CHARS = 25           # an added line needs this many characters (trimmed) to count
MOVED_BOILERPLATE_FILES = 20   # a line found in this many distinct (project, file) pairs is boilerplate
MOVED_DISCOUNT_SHARE = 0.5     # analysis discounts a file once this share of its lines moved

RULE_ORDER = ("merge", "squash", "promotion", "copy")  # first match wins

_PAIR_BITS = 32
_PAIR_MASK = (1 << _PAIR_BITS) - 1

Progress = Callable[[str], None]


# ── Pure helpers ─────────────────────────────────────────────────────────────

def parse_parents(parent_ids: Optional[str]) -> list[str]:
    return [p.strip() for p in (parent_ids or "").split(",") if p.strip()]


def utc_naive(value) -> datetime:
    """Comparable timestamp: DuckDB returns naive, BigQuery tz-aware datetimes."""
    if value is None:
        return datetime.max
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def fingerprint(text: str) -> int:
    """Deterministic 64-bit fingerprint (never the per-process ``hash()``)."""
    return int.from_bytes(
        hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8).digest(), "big"
    )


def file_signature(diff: Optional[str], additions, deletions) -> str:
    """Content signature of one file change, for the copy rule.

    md5 of the '+'/'-' lines, joined by newline, ``+++ `` / ``--- `` header
    lines excluded. Context lines and hunk headers are ignored so shifted line
    numbers do not break a match. A missing (or empty: binary, oversized)
    diff falls back to ``additions:deletions``.
    """
    if not diff:
        return f"{additions or 0}:{deletions or 0}"
    changed = [
        line for line in diff.split("\n")
        if line[:1] in ("+", "-") and not line.startswith(("+++ ", "--- "))
    ]
    return hashlib.md5("\n".join(changed).encode("utf-8", "replace")).hexdigest()


def commit_signature(files: dict[str, str]) -> str:
    """Per-file signatures (``path=sig``), sorted by path, joined."""
    return "|".join(f"{path}={files[path]}" for path in sorted(files))


def qualifying_fingerprints(diff: Optional[str]) -> set[int]:
    """Distinct fingerprints of the added lines that can count as moved code:
    '+' lines (not '+++'), trimmed, at least MOVED_MIN_CHARS long."""
    out: set[int] = set()
    if not diff:
        return out
    for line in diff.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            text = line[1:].strip()
            if len(text) >= MOVED_MIN_CHARS:
                out.add(fingerprint(text))
    return out


def is_long_lived(branch: Optional[str]) -> bool:
    return bool(branch) and LONG_LIVED_BRANCH_RE.search(branch) is not None


class _FirstSeen:
    """First occurrence of each fingerprint, and the boilerplate set.

    ``first[fp]`` packs ``(commit rank, file id)`` into one int; the smallest
    wins, i.e. earliest ``(committed_date, sha)`` and, inside one commit, the
    lowest file id (deterministic, arbitrary). A fingerprint that shows up in
    MOVED_BOILERPLATE_FILES distinct files is boilerplate: its entries are
    dropped and it is ignored from then on. ``others`` holds the distinct
    files other than the first one, and only exists for fingerprints seen in
    more than one file, which keeps the common case at one int per fingerprint.
    """

    def __init__(self) -> None:
        self.first: dict[int, int] = {}
        self.others: dict[int, set[int]] = {}
        self.boiler: set[int] = set()

    def add(self, fp: int, rank: int, pair: int) -> None:
        if fp in self.boiler:
            return
        packed = (rank << _PAIR_BITS) | pair
        cur = self.first.get(fp)
        if cur is None:
            self.first[fp] = packed
            return
        cur_pair = cur & _PAIR_MASK
        others = self.others.get(fp)
        if pair == cur_pair:
            if packed < cur:
                self.first[fp] = packed
            return
        if others is not None and pair in others:
            if packed < cur:
                others.discard(pair)
                others.add(cur_pair)
                self.first[fp] = packed
            return
        if others is None:
            others = self.others[fp] = set()
        if packed < cur:
            others.add(cur_pair)
            self.first[fp] = packed
        else:
            others.add(pair)
        if len(others) + 1 >= MOVED_BOILERPLATE_FILES:
            del self.first[fp], self.others[fp]
            self.boiler.add(fp)

    def moved(self, fp: int, rank: int, pair: int) -> bool:
        """Was *fp* first seen in a different commit AND a different file?"""
        first = self.first.get(fp)
        if first is None:  # never seen in pass 1 (data changed in between)
            return False
        return (first >> _PAIR_BITS) != rank and (first & _PAIR_MASK) != pair


# ── Loading ──────────────────────────────────────────────────────────────────

@dataclass
class _Commit:
    project: str
    email: str
    title: str
    lines: int
    parents: list[str]
    rank: int = 0


@dataclass
class _MR:
    merged: bool
    source_branch: Optional[str]
    merge_sha: Optional[str] = None
    squash_sha: Optional[str] = None

    @property
    def has_sha(self) -> bool:
        return bool(self.merge_sha or self.squash_sha)


def _load_commits(storage) -> dict[str, _Commit]:
    commits: dict[str, _Commit] = {}
    when: dict[str, datetime] = {}
    for r in storage.iter_query(
        "SELECT commit_sha, project_path, author_email, title, committed_date, "
        "authored_date, additions, deletions, parent_ids FROM commits"
    ):
        sha = r["commit_sha"]
        commits[sha] = _Commit(
            project=r["project_path"] or "",
            email=(r["author_email"] or "").lower(),
            title=r["title"] or "",
            lines=(r["additions"] or 0) + (r["deletions"] or 0),
            parents=parse_parents(r["parent_ids"]),
        )
        when[sha] = utc_naive(r["committed_date"] or r["authored_date"])
    # Rank = position in (committed_date, sha) order: "earliest" everywhere.
    for rank, sha in enumerate(sorted(commits, key=lambda s: (when[s], s))):
        commits[sha].rank = rank
    return commits


_PR_URL_RE = re.compile(r"^https?://[^/]+/(.+?)/pull/\d+")


def _mr_project(project_id, web_url, project_name) -> str:
    """Identity of an MR's project, comparable with ``mr_commits.project_id``.

    GitLab: the numeric project id, on both tables. GitHub: ``project_id`` is
    empty and ``mr_commits`` carries the ``owner/name`` slug, which is the path
    in the PR's ``web_url``. Only when that is missing too does it fall back to
    the short name (``~name``), which cannot tell two same-named repos apart.
    Never the short name alone when something better exists: iids restart in
    every project, so two projects named ``api`` would merge into one MR.
    """
    if project_id:
        return str(project_id)
    m = _PR_URL_RE.match(web_url or "")
    if m:
        return m.group(1).lower()
    return "~" + (project_name or "")


def _load_merge_requests(storage) -> tuple[dict, dict]:
    """``({mr key: _MR}, {mr key: {commit shas}})``, keyed ``(project, iid)``.

    Re-extracting a period inserts the same MR again (the extractors do not
    update rows), so rows are grouped per MR: the newest row gives state and
    branch, and the newest non-NULL value of each sha column wins, so an old
    NULL row never hides a filled one (nor a newer NULL row an older fill).
    """
    rows = list(storage.iter_query(
        "SELECT mr_iid, project_id, project_name, web_url, state, source_branch, "
        "merge_commit_sha, squash_commit_sha, extracted_at FROM merge_requests"
    ))
    rows.sort(key=lambda r: utc_naive(r["extracted_at"]), reverse=True)  # newest first
    mrs: dict[tuple[str, str], _MR] = {}
    for r in rows:
        key = (_mr_project(r["project_id"], r["web_url"], r["project_name"]), str(r["mr_iid"]))
        mr = mrs.get(key)
        if mr is None:
            mr = mrs[key] = _MR(merged=r["state"] == "merged",
                                source_branch=r["source_branch"])
        mr.merge_sha = mr.merge_sha or r["merge_commit_sha"] or None
        mr.squash_sha = mr.squash_sha or r["squash_commit_sha"] or None

    projects = {project for project, _ in mrs}
    links: dict[tuple[str, str], set[str]] = {}
    for r in storage.iter_query("SELECT mr_iid, project_id, commit_sha FROM mr_commits"):
        pid = str(r["project_id"] or "")
        project = pid if pid in projects else pid.lower()
        if project not in projects:
            project = "~" + pid.rsplit("/", 1)[-1]  # MR rows without a web_url
        links.setdefault((project, str(r["mr_iid"])), set()).add(r["commit_sha"])
    return mrs, links


def _scan_diffs(storage, commits: dict[str, _Commit]) -> Iterable[tuple]:
    """Stream ``(sha, file_path, diff, additions, deletions)`` for known commits."""
    for r in storage.iter_query(
        "SELECT commit_sha, file_path, diff, additions, deletions FROM diffs"
    ):
        if r["commit_sha"] in commits:
            yield (r["commit_sha"], r["file_path"] or "", r["diff"],
                   r["additions"], r["deletions"])


# ── The rules ────────────────────────────────────────────────────────────────

def _rule_merge(commits: dict[str, _Commit]) -> set[str]:
    return {sha for sha, c in commits.items()
            if len(c.parents) >= 2 and c.parents[1] in commits}


def _rule_squash_and_promotion(commits, mrs, links) -> tuple[set[str], set[str]]:
    squash: set[str] = set()
    promotion: set[str] = set()
    for key, mr in mrs.items():
        if not mr.merged:
            continue
        own = links.get(key, set())
        # The schema does not record the platform, so both columns are candidates
        # and the one-parent test below tells a real merge commit from a squash.
        # A merge_commit_sha that is one of the MR's own commits is a
        # fast-forward head: real work, stored once, never a duplicate.
        candidates = {mr.squash_sha}
        if mr.merge_sha not in own:
            candidates.add(mr.merge_sha)
        for sha in candidates:
            c = commits.get(sha) if sha else None
            if c is None:
                continue
            # The PR's own stored commits. Promotion needs only that some of the
            # work is stored elsewhere (a fork PR from `main` has none, so its
            # squash commit is the only copy); squash coverage counts real
            # commits only, not merges of the base branch into the PR.
            branch = [commits[s] for s in own if s != sha and s in commits]
            if is_long_lived(mr.source_branch) and branch:
                promotion.add(sha)
            if len(c.parents) != 1 or c.lines <= 0:
                continue
            real = [b for b in branch if len(b.parents) < 2]
            if real and sum(b.lines for b in real) >= SQUASH_COVERAGE * c.lines:
                squash.add(sha)
    return squash, promotion


@dataclass
class Result:
    duplicates: dict[str, str]          # commit_sha -> rule
    moved: list[dict]                   # file_moved_share rows
    commits: int
    merged_mrs: int
    merged_mrs_with_sha: int

    def counts(self) -> dict[str, int]:
        out = {rule: 0 for rule in RULE_ORDER}
        for why in self.duplicates.values():
            out[why] += 1
        return out


def compute(storage, progress: Progress = lambda msg: None) -> Result:
    """Apply the five rules to everything in *storage*. Writes nothing."""
    commits = _load_commits(storage)
    progress(f"  {len(commits):,} commits loaded")
    mrs, links = _load_merge_requests(storage)
    merged = [m for m in mrs.values() if m.merged]
    with_sha = sum(1 for m in merged if m.has_sha)
    progress(f"  {len(merged):,} merged MRs/PRs, {with_sha:,} with a merge/squash sha")

    matched = {
        "merge": _rule_merge(commits),
        "copy": set(),
    }
    matched["squash"], matched["promotion"] = _rule_squash_and_promotion(commits, mrs, links)

    # ── Pass 1 over diffs: copy signatures + first sighting of every line ──
    file_sigs: dict[str, dict[str, str]] = {}
    seen = _FirstSeen()
    pair_ids: dict[tuple[str, str], int] = {}
    rows = 0
    for sha, path, diff, adds, dels in _scan_diffs(storage, commits):
        c = commits[sha]
        file_sigs.setdefault(sha, {})[path] = file_signature(diff, adds, dels)
        pair = pair_ids.setdefault((c.project, path), len(pair_ids))
        for fp in qualifying_fingerprints(diff):
            seen.add(fp, c.rank, pair)
        rows += 1
        if rows % 200_000 == 0:
            progress(f"  pass 1: {rows:,} file diffs")
    progress(f"  pass 1 done: {rows:,} file diffs, {len(seen.first):,} distinct lines, "
             f"{len(seen.boiler):,} boilerplate lines ignored")

    # Rule 4: the earliest (committed_date, sha) of an identical group is the original.
    commit_key: dict[str, bytes] = {}
    earliest: dict[bytes, int] = {}
    for sha, files in file_sigs.items():
        c = commits[sha]
        key = hashlib.md5("\x00".join(
            (c.project, c.email, c.title, commit_signature(files))
        ).encode("utf-8", "replace")).digest()
        commit_key[sha] = key
        if key not in earliest or c.rank < earliest[key]:
            earliest[key] = c.rank
    matched["copy"] = {sha for sha, key in commit_key.items()
                       if commits[sha].rank > earliest[key]}
    del file_sigs

    duplicates: dict[str, str] = {}
    for rule in RULE_ORDER:
        for sha in matched[rule]:
            duplicates.setdefault(sha, rule)

    # ── Pass 2: moved share per (commit, file) ──
    moved_rows: dict[tuple[str, str], dict] = {}
    for sha, path, diff, _a, _d in _scan_diffs(storage, commits):
        c = commits[sha]
        pair = pair_ids.setdefault((c.project, path), len(pair_ids))
        fps = [fp for fp in qualifying_fingerprints(diff) if fp not in seen.boiler]
        if not fps:
            continue
        moved = sum(1 for fp in fps if seen.moved(fp, c.rank, pair))
        if moved:
            moved_rows[(sha, path)] = {
                "commit_sha": sha, "file_path": path,
                "qualifying_lines": len(fps), "moved_lines": moved,
                "moved_share": moved / len(fps),
                "rules_version": DEDUPE_RULES_VERSION,
            }
    progress(f"  pass 2 done: {len(moved_rows):,} files with moved lines")

    return Result(
        duplicates=duplicates,
        moved=sorted(moved_rows.values(), key=lambda r: (r["commit_sha"], r["file_path"])),
        commits=len(commits),
        merged_mrs=len(merged),
        merged_mrs_with_sha=with_sha,
    )


def write(storage, result: Result, now: Optional[datetime] = None) -> None:
    """Replace the three result tables. The run marker goes first (emptied) and
    last (filled), so a crash in between reads as "never ran", not as a clean run."""
    now = now or datetime.now(timezone.utc)
    storage.replace_table("dedupe_runs", [])
    storage.replace_table("duplicate_commits", [
        {"commit_sha": sha, "why": why, "rules_version": DEDUPE_RULES_VERSION}
        for sha, why in sorted(result.duplicates.items())
    ])
    storage.replace_table("file_moved_share", result.moved)
    storage.replace_table("dedupe_runs", [{
        "rules_version": DEDUPE_RULES_VERSION,
        "run_at": _iso(now),
        "commits": result.commits,
        "merged_mrs": result.merged_mrs,
        "merged_mrs_with_sha": result.merged_mrs_with_sha,
    }])


# ── Reading the last run (analyze, dedupe) ───────────────────────────────────

def last_run(storage) -> Optional[dict]:
    """The completion marker of the last dedupe run, or None if it never ran
    (or the table does not exist yet, e.g. an old read-only database)."""
    try:
        rows = storage.query(
            "SELECT rules_version, run_at, commits, merged_mrs, merged_mrs_with_sha "
            "FROM dedupe_runs ORDER BY run_at DESC LIMIT 1"
        )
    except Exception as exc:
        if not is_table_missing(exc):
            raise
        return None
    return rows[0] if rows else None
