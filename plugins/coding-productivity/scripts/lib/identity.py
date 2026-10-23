"""
Automatic identity resolution at analysis time.

``dedup.find_duplicates`` only suggests merges to a human. This module decides,
for every query, which stored ``(author_name, author_email)`` pairs are one
person, so developer counts do not depend on how many addresses someone
commits from.

Everything works on the values *as stored*. With anonymization on, those are
HMAC hashes, and the same raw name always hashes to the same value, so
"same name" and "same email" are plain equality on stored values.

Rules, in order:

1. ``IDENTITY_MERGES`` (``alias:canonical`` emails) are applied first.
2. With ``IDENTITY_AUTO_MERGE`` on, two emails used with the same author name
   are one person, and one email used with two names is one person
   (union-find over names and emails).
3. A name in ``dedup.GENERIC_NAMES`` (``root``, ``admin``...) never links
   anything, raw or hashed.
4. Two emails listed together in ``IDENTITY_NO_MERGE`` are never joined by
   rule 2.
5. A person whose every stored name is generic is a non-person account and
   is excluded from the analysis, as bots are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import anonymize
from .dedup import GENERIC_NAMES, normalize_name


# ── Generic names, raw and hashed ────────────────────────────────────────────

def generic_name_hashes(key: Optional[bytes]) -> set[str]:
    """Stored forms of the generic names under *key*.

    ``anonymize.pseudonymize`` lowercases and strips the value before hashing,
    but does not remove separators, so the separator variants of names such as
    ``no-reply`` are hashed too.
    """
    if not key:
        return set()
    forms: set[str] = set()
    for name in GENERIC_NAMES:
        for variant in (name, name.replace("-", " "), name.replace("-", "_"),
                        name.replace("-", ".")):
            forms.add(anonymize.pseudonymize(variant, key))
    return forms


def is_generic_name(name: Optional[str], hashed_generics: set[str] = frozenset()) -> bool:
    """True when the stored *name* is a generic account name (raw or hashed)."""
    if not name or not name.strip():
        return False
    return (normalize_name(name) in GENERIC_NAMES
            or name.strip().lower() in hashed_generics)


# ── Result ───────────────────────────────────────────────────────────────────

@dataclass
class Person:
    """One resolved person (or non-person account) and its stored identities."""
    key: str                                  # stored email of the main identity
    identities: list[tuple[str, str, int]]    # (name, email, commits), most commits first
    non_person: bool = False
    auto_merged: bool = False                 # two or more emails joined by a rule

    @property
    def emails(self) -> list[str]:
        seen: list[str] = []
        for _name, email, _n in self.identities:
            if email not in seen:
                seen.append(email)
        return seen

    @property
    def commits(self) -> int:
        return sum(n for _name, _email, n in self.identities)


@dataclass
class People:
    persons: list[Person] = field(default_factory=list)
    by_email: dict[str, Person] = field(default_factory=dict)

    def key(self, email: Optional[str]) -> Optional[str]:
        """Person key of a stored email; an unknown email is its own person."""
        if email is None:
            return None
        person = self.by_email.get(email)
        return person.key if person else email

    def non_person_emails(self) -> list[str]:
        """Stored emails of non-person accounts, to exclude from queries."""
        return sorted(e for p in self.persons if p.non_person for e in p.emails)

    def auto_merged(self) -> list[Person]:
        return [p for p in self.persons if p.auto_merged and not p.non_person]

    def non_persons(self) -> list[Person]:
        return [p for p in self.persons if p.non_person]


# ── Union-find ───────────────────────────────────────────────────────────────

class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:  # path compression
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # The smaller string wins, so the result does not depend on order.
            self._parent[max(ra, rb)] = min(ra, rb)


# ── Resolution ───────────────────────────────────────────────────────────────

def resolve(
    pairs: Iterable[tuple[Optional[str], Optional[str], int]],
    *,
    merges: Optional[dict[str, str]] = None,
    no_merge: Iterable[tuple[str, str]] = (),
    auto: bool = True,
    key: Optional[bytes] = None,
) -> People:
    """Group stored ``(author_name, author_email, commits)`` rows into people.

    *merges* and *no_merge* hold emails as written in the config (raw); each
    is matched against the stored value, and against its pseudonym when *key*
    is given. Pairs without an email are ignored.
    """
    # Aggregate per exact stored (name, email).
    counts: dict[tuple[str, str], int] = {}
    for name, email, n in pairs:
        if not email:
            continue
        pair = (name or "", email)
        counts[pair] = counts.get(pair, 0) + (n or 0)
    if not counts:
        return People()

    stored_by_lower: dict[str, str] = {}   # lowercase email -> a stored email
    for _name, email in counts:
        stored_by_lower.setdefault(email.lower(), email)

    def stored_node(email: str) -> Optional[str]:
        """Node (lowercase stored email) for a config email, if present."""
        for form in (email.strip().lower(),
                     anonymize.pseudonymize(email, key) if key else None):
            if form and form.lower() in stored_by_lower:
                return form.lower()
        return None

    uf = _UnionFind()
    for lower in stored_by_lower:
        uf.find("e:" + lower)

    # 1. Explicit merges.
    for alias, canonical in (merges or {}).items():
        a, c = stored_node(alias), stored_node(canonical)
        if a and c:
            uf.union("e:" + a, "e:" + c)
    explicit_root = {lower: uf.find("e:" + lower) for lower in stored_by_lower}

    # 2-4. Automatic merge through shared names.
    hashed_generics = generic_name_hashes(key)
    if auto:
        blocked: list[tuple[str, str]] = []
        for left, right in no_merge:
            a, b = stored_node(left), stored_node(right)
            if a and b and a != b:
                blocked.append(("e:" + a, "e:" + b))

        def allowed(x: str, y: str) -> bool:
            rx, ry = uf.find(x), uf.find(y)
            if rx == ry:
                return True
            for a, b in blocked:
                ra, rb = uf.find(a), uf.find(b)
                if (ra, rb) in ((rx, ry), (ry, rx)):
                    return False
            return True

        for name, email in sorted(counts):
            if not name.strip() or is_generic_name(name, hashed_generics):
                continue
            node, name_node = "e:" + email.lower(), "n:" + name.strip().lower()
            if allowed(node, name_node):
                uf.union(node, name_node)

    # Components.
    components: dict[str, list[tuple[str, str, int]]] = {}
    for (name, email), n in counts.items():
        components.setdefault(uf.find("e:" + email.lower()), []).append((name, email, n))

    people = People()
    for root in sorted(components):
        idents = sorted(components[root], key=lambda i: (-i[2], i[1], i[0]))
        roots = {explicit_root[email.lower()] for _n, email, _c in idents}
        person = Person(
            key=idents[0][1],
            identities=idents,
            non_person=all(is_generic_name(n, hashed_generics) for n, _e, _c in idents),
            auto_merged=len(roots) > 1,
        )
        people.persons.append(person)
        for _name, email, _n in idents:
            people.by_email[email] = person
    return people


# ── Report ───────────────────────────────────────────────────────────────────

def _describe(identity: tuple[str, str, int]) -> str:
    name, email, n = identity
    return f"{name or '(no name)'} <{email}> ({n:,} commits)"


def format_report(people: People, *, anonymized: bool = False,
                  limit: int = 50) -> list[str]:
    """Lines saying which identities were merged or excluded, and how to undo."""
    lines: list[str] = []
    merged = people.auto_merged()
    if merged:
        n_identities = sum(len(p.emails) for p in merged)
        lines.append(
            f"identity: {len(merged)} automatic merge(s), {n_identities} stored "
            f"emails into {len(merged)} people:"
        )
        for i, person in enumerate(merged[:limit], 1):
            lines.append(f"  {i}. " + "; ".join(_describe(x) for x in person.identities))
        if len(merged) > limit:
            lines.append(f"  ... and {len(merged) - limit} more")
        # Emails differing only by case are one node: pair two that really differ.
        first = merged[0].emails
        other = next(e for e in first if e.lower() != first[0].lower())
        lines.append(
            "  A wrong merge? Add the two emails to IDENTITY_NO_MERGE, e.g. "
            f"IDENTITY_NO_MERGE={first[0]}:{other} "
            "(IDENTITY_AUTO_MERGE=false switches automatic merging off)."
        )
        if anonymized:
            lines.append(
                "  (anonymization is on, so these are pseudonyms; to check a merge "
                "run /coding-productivity:developers, which resolves them locally)"
            )
    excluded = people.non_persons()
    if excluded:
        lines.append(
            f"identity: {len(excluded)} non-person account(s) excluded from developer "
            "counts and totals: "
            + "; ".join(_describe(x) for p in excluded[:limit] for x in p.identities)
        )
    return lines
