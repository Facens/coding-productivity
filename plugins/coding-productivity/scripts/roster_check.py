#!/usr/bin/env python3
"""
Roster check: report who counts as a developer, without changing anything.

``EXCLUDED_DEVELOPERS`` is an exact per-email list and ``analyze`` applies it
before identities are grouped into people, so an excluded person who commits
from a new address (a GitHub noreply address, a second work email) silently
counts as a developer. New people also start counting with nobody looking at
them, and an alias that is not merged splits one person in two. This check
reads the stored commits (``REPOS`` applied, the exclusion list not, all
dates), resolves identities exactly as ``analyze`` does and reports:

1. **To exclude (leak)** -- a person with at least one email in
   ``EXCLUDED_DEVELOPERS`` and at least one email not in it.
2. **New, to confirm** -- counted people whose first commit in the whole
   dataset falls inside ``--since`` .. ``--until``.
3. **Merge candidates** -- pairs ``lib/dedup.find_duplicates`` suggests that
   are not already one person, not in ``IDENTITY_NO_MERGE``, and not both
   excluded.

The config is never modified. The exit code is 0 whenever the check ran, with
or without findings, so a periodic wrapper does not read findings as failure;
it is non-zero only for real errors (unreadable config, unreachable storage,
bad dates). ``--output FILE`` writes the report there only when there are
findings (a stale file from an earlier run is removed when there are none).

The period follows ``analyze``: ``--since`` inclusive, ``--until`` exclusive,
on ``DATE_FIELD``.

With anonymization on, stored values are HMAC hashes; if
``.coding-productivity/developer_mapping.json`` exists next to the config,
hashes are shown as the real name and email. Membership in
``EXCLUDED_DEVELOPERS`` is the same exact comparison against the stored value
that ``analyze`` makes, so with anonymization on the list must hold the hashes.

Usage::

    python roster_check.py --config .coding-productivity.env --since 2026-10-01 --until 2026-11-01
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze
from lib import anonymize, bots, dialect, identity
from lib.config import Config
from lib.dedup import find_duplicates
from lib.storage import get_storage, Storage

MAPPING_RELATIVE = Path(".coding-productivity") / "developer_mapping.json"


# ── Data ─────────────────────────────────────────────────────────────────────

@dataclass
class Row:
    """One stored ``(author_name, author_email)`` and its commit counts."""
    name: str
    email: str
    total: int       # all dates
    period: int      # inside since..until
    before: int      # before since


@dataclass
class Leak:
    person: identity.Person
    title: str                                    # the person's main identity
    covered: list[str]                            # emails already excluded, as shown
    uncovered: list[tuple[str, str, int, int]]    # (stored email, label, total, period)


@dataclass
class NewPerson:
    label: str
    emails: list[str]
    period: int


@dataclass
class Candidate:
    a: dict
    b: dict
    reason: str
    excluded_side: Optional[dict] = None    # the side already in EXCLUDED_DEVELOPERS


@dataclass
class Report:
    since: str
    until: str
    leaks: list[Leak] = field(default_factory=list)
    new: list[NewPerson] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    leak_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    merge_lines: list[str] = field(default_factory=list)
    anonymized: bool = False
    mapped: bool = False

    @property
    def has_findings(self) -> bool:
        return bool(self.leaks or self.new or self.candidates)


# ── Reading ──────────────────────────────────────────────────────────────────

def load_rows(storage: Storage, config, since: str, until: str) -> list[Row]:
    """Stored identities with commit counts: ``REPOS`` filter, no exclusion
    filter, all dates; counts split into before / inside the period."""
    date_col = analyze._date_col(config)
    s = dialect.timestamp_param("$since")
    u = dialect.timestamp_param("$until")
    params: dict = {"since": since, "until": until}
    repo_clause, repo_params = analyze._repo_filter(config)
    where = ""
    if repo_clause:
        where = "WHERE " + repo_clause
        params.update(repo_params)
    rows = storage.query(f"""
        SELECT c.author_name AS author_name, c.author_email AS author_email,
               COUNT(DISTINCT c.commit_sha) AS commits,
               COUNT(DISTINCT CASE WHEN {date_col} >= {s} AND {date_col} < {u}
                                   THEN c.commit_sha END) AS in_period,
               COUNT(DISTINCT CASE WHEN {date_col} < {s}
                                   THEN c.commit_sha END) AS before_period
        FROM commits c {where}
        GROUP BY c.author_name, c.author_email""", params)
    return [Row(r["author_name"] or "", r["author_email"], int(r["commits"] or 0),
                int(r["in_period"] or 0), int(r["before_period"] or 0))
            for r in rows if r["author_email"]]


# ── Display ──────────────────────────────────────────────────────────────────

class _Display:
    """Stored value -> what to show: the real name/email when the mapping has it."""

    def __init__(self, mapping: Optional[dict] = None):
        self.mapping = mapping or {}

    def email(self, stored: str) -> str:
        return (self.mapping.get(stored) or {}).get("email") or stored

    def name(self, stored_name: str, stored_email: str) -> str:
        info = self.mapping.get(stored_name) or self.mapping.get(stored_email) or {}
        return info.get("name") or stored_name


def _label(display: _Display, name: str, email: str) -> str:
    n = display.name(name, email)
    return f"{n or '(no name)'} <{display.email(email)}>"


# ── The check ────────────────────────────────────────────────────────────────

def _load_bot_overrides(config) -> None:
    """Same override list the extractors load (their ``Bot override added``
    lines are not part of this report)."""
    overrides = getattr(config, "BOT_OVERRIDES", None) or []
    if overrides:
        with contextlib.redirect_stdout(io.StringIO()):
            bots.load_overrides(overrides)


def run_check(rows: list[Row], config, since: str, until: str,
              mapping: Optional[dict] = None) -> Report:
    """Resolve *rows* like ``analyze.load_people`` (but without the exclusion
    filter) and build the three sections."""
    anonymized = bool(getattr(config, "ANONYMIZATION_ENABLED", False))
    key = getattr(config, "PSEUDONYMIZATION_KEY", None) if anonymized else None
    display = _Display(mapping if anonymized else None)
    report = Report(since=since, until=until, anonymized=anonymized,
                    mapped=bool(anonymized and mapping))
    if not rows:
        return report

    excluded = set(getattr(config, "EXCLUDED_DEVELOPERS", None) or [])
    people = identity.resolve(
        [(r.name, r.email, r.total) for r in rows],
        merges=getattr(config, "IDENTITY_MERGES", None) or {},
        no_merge=getattr(config, "IDENTITY_NO_MERGE", None) or [],
        auto=getattr(config, "IDENTITY_AUTO_MERGE", True),
        key=key,
    )
    by_email: dict[str, list[Row]] = {}
    for r in rows:
        by_email.setdefault(r.email, []).append(r)

    def counted(person: identity.Person, attr: str) -> int:
        return sum(getattr(r, attr) for e in person.emails for r in by_email.get(e, []))

    def is_bot_person(person: identity.Person) -> bool:
        """Bots are dropped at extraction, from the real values. Detectable here
        only on real values (anonymization off, or a mapping)."""
        return all(bots.is_bot(name=display.name(n, e), email=display.email(e))
                   for n, e, _c in person.identities)

    # 1. Leaks.
    leaked_keys: set[str] = set()
    for person in people.persons:
        if person.non_person:
            continue
        covered = [e for e in person.emails if e in excluded]
        open_ = [e for e in person.emails if e not in excluded]
        if not covered or not open_:
            continue
        leaked_keys.add(person.key)
        uncovered = []
        for e in open_:
            rs = by_email[e]
            best = max(rs, key=lambda r: r.total)
            uncovered.append((e, _label(display, best.name, e),
                              sum(r.total for r in rs), sum(r.period for r in rs)))
        uncovered.sort(key=lambda u: (-u[2], u[0]))
        top = person.identities[0]
        report.leaks.append(Leak(person, _label(display, top[0], top[1]),
                                 [display.email(e) for e in covered], uncovered))
    report.leaks.sort(key=lambda l: -l.person.commits)

    # 2. New people.
    for person in people.persons:
        if person.non_person or person.key in leaked_keys:
            continue
        if all(e in excluded for e in person.emails) or is_bot_person(person):
            continue
        if counted(person, "before") == 0 and counted(person, "period") > 0:
            top = person.identities[0]
            report.new.append(NewPerson(
                label=_label(display, top[0], top[1]),
                emails=[display.email(e) for e in person.emails],
                period=counted(person, "period")))
    report.new.sort(key=lambda p: (-p.period, p.label))

    # 3. Merge candidates, on real values when they are known.
    devs = []
    for r in rows:
        person = people.by_email[r.email]
        name = display.name(r.name, r.email)
        if person.non_person or not name.strip() or is_bot_person(person):
            continue
        devs.append({"name": name, "email": display.email(r.email),
                     "commit_count": r.total, "stored_email": r.email})
    # One entry per email (find_duplicates pairs by email): keep the busiest name.
    best_by_email: dict[str, dict] = {}
    for d in devs:
        cur = best_by_email.get(d["stored_email"])
        if cur is None or d["commit_count"] > cur["commit_count"]:
            best_by_email[d["stored_email"]] = d
    blocked = list(getattr(config, "IDENTITY_NO_MERGE", None) or [])

    def matches(cfg_email: str, d: dict) -> bool:
        form = cfg_email.strip().lower()
        stored = d["stored_email"].lower()
        return (form in (stored, d["email"].lower())
                or bool(key and anonymize.pseudonymize(cfg_email, key).lower() == stored))

    for a, b, reason in find_duplicates(list(best_by_email.values())):
        if people.by_email[a["stored_email"]] is people.by_email[b["stored_email"]]:
            continue
        if any((matches(l, a) and matches(r, b)) or (matches(l, b) and matches(r, a))
               for l, r in blocked):
            continue
        a_ex, b_ex = a["stored_email"] in excluded, b["stored_email"] in excluded
        if a_ex and b_ex:
            continue
        report.candidates.append(Candidate(a, b, reason, a if a_ex else b if b_ex else None))
    report.candidates.sort(key=lambda c: -(c.a["commit_count"] + c.b["commit_count"]))
    _build_lines(report)
    return report


def _build_lines(report: Report) -> None:
    # Paste-ready values are the stored ones: that is what analyze compares.
    if report.leaks:
        stored = [u[0] for lk in report.leaks for u in lk.uncovered]
        report.leak_lines = [
            "EXCLUDED_DEVELOPERS: add these emails (existing entries stay):",
            ",".join(stored),
        ]
    merges, no_merge = [], []
    for c in report.candidates:
        no_merge.append(f"{c.a['email']}:{c.b['email']}")
        if c.excluded_side is None:
            alias, canon = sorted((c.a, c.b), key=lambda d: (d["commit_count"], d["email"]))
            merges.append(f"{alias['email']}:{canon['email']}")
    if merges:
        report.merge_lines.append("If they are the same person, add to IDENTITY_MERGES (alias:canonical):")
        report.merge_lines.append("IDENTITY_MERGES=" + ",".join(merges))
    if report.candidates:
        report.merge_lines.append("If they are different people, add to IDENTITY_NO_MERGE:")
        report.merge_lines.append("IDENTITY_NO_MERGE=" + ",".join(no_merge))


# ── Rendering ────────────────────────────────────────────────────────────────

def render(report: Report) -> list[str]:
    head = (f"Roster check {report.since} .. {report.until} "
            "(since inclusive, until exclusive)")
    if not report.has_findings:
        return [head, "No findings."]
    out = [head, ""]
    if report.leaks:
        out.append(f"1. TO EXCLUDE: {len(report.leaks)} person(s) partly in "
                   "EXCLUDED_DEVELOPERS; the other emails count as developers")
        for i, lk in enumerate(report.leaks, 1):
            out.append(f"  {i}. {lk.title}")
            out.append(f"     excluded: {', '.join(lk.covered)}")
            for _stored, label, total, period in lk.uncovered:
                out.append(f"     NOT excluded: {label}  {total:,} commits "
                           f"({period:,} in period)")
        out += [f"  {l}" for l in report.leak_lines]
        out.append("")
    if report.new:
        out.append(f"2. NEW, TO CONFIRM: {len(report.new)} person(s) whose first commit "
                   "is in the period. Confirm each is a developer, or add their emails "
                   "to EXCLUDED_DEVELOPERS.")
        for i, p in enumerate(report.new, 1):
            out.append(f"  {i}. {p.label}  {p.period:,} commits in period"
                       + (f"  (all emails: {', '.join(p.emails)})" if len(p.emails) > 1 else ""))
        out.append("")
    if report.candidates:
        out.append(f"3. MERGE CANDIDATES: {len(report.candidates)} pair(s) not yet one person")
        for i, c in enumerate(report.candidates, 1):
            out.append(f"  {i}. [{c.reason}]")
            for side in (c.a, c.b):
                mark = "  (excluded)" if side is c.excluded_side else ""
                out.append(f"     {side['name']} <{side['email']}>  "
                           f"{side['commit_count']:,} commits{mark}")
            if c.excluded_side is not None:
                other = c.b if c.excluded_side is c.a else c.a
                out.append("     one side is excluded: if it is the same person, add "
                           f"{other['stored_email']} to EXCLUDED_DEVELOPERS")
        out += [f"  {l}" for l in report.merge_lines]
        out.append("")
    if report.anonymized and not report.mapped:
        out.append("Anonymization is on and no developer mapping was found, so values are "
                   "hashes. Run /coding-productivity:developers to resolve them.")
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────

def _parse_date(value: str, flag: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"Error: {flag} must be YYYY-MM-DD, got {value!r}.")
    return value


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report who counts as a developer: exclusion leaks, new people, "
                    "merge candidates. Report only; the config is never modified.")
    parser.add_argument("--config", required=True, help="Path to .coding-productivity.env.")
    parser.add_argument("--since", required=True, help="Period start, inclusive (YYYY-MM-DD).")
    parser.add_argument("--until", required=True, help="Period end, exclusive (YYYY-MM-DD).")
    parser.add_argument("--output", help="Write the report here, only when there are findings.")
    args = parser.parse_args(argv)

    try:
        since = _parse_date(args.since, "--since")
        until = _parse_date(args.until, "--until")
        if since >= until:
            raise SystemExit("Error: --since must be before --until.")
        config_path = Path(args.config)
        if not config_path.is_file():
            raise SystemExit(f"Error: config file not found: {args.config}")
        config = Config(args.config)
        _load_bot_overrides(config)
        mapping = None
        if config.ANONYMIZATION_ENABLED:
            mapping = anonymize.load_mapping(
                config_path.resolve().parent / MAPPING_RELATIVE) or None
        storage = get_storage(config)
        try:
            rows = load_rows(storage, config, since, until)
        finally:
            storage.close()
        report = run_check(rows, config, since, until, mapping)
    except SystemExit:
        raise
    except Exception as exc:  # real errors: unreadable config, storage, query
        print(f"Error: roster check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    text = "\n".join(render(report)).rstrip() + "\n"
    print(text, end="", flush=True)
    if args.output:
        out = Path(args.output)
        try:
            if report.has_findings:
                out.write_text(text, encoding="utf-8")
            elif out.exists():
                out.unlink()
        except OSError as exc:
            print(f"Error: cannot write {args.output}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
