"""
Bot account detection heuristics.

Provides static lists and pattern-based matching for known CI/CD bots,
GitHub Apps, and service accounts so they can be excluded from developer
productivity metrics.
"""

from __future__ import annotations

import re
from typing import Sequence


# ── Known bot identities ──────────────────────────────────────────────────────

BOT_NAMES: set[str] = {
    "dependabot[bot]",
    "renovate[bot]",
    "github-actions[bot]",
    "GitLab CI",
    "Administrator",
    "Mergify",
    "Codecov",
    "Snyk Bot",
    "greenkeeper[bot]",
    "semantic-release-bot",
    "web-flow",  # GitHub web-UI commits
}

BOT_EMAILS: set[str] = {
    "noreply@github.com",
    "49699333+dependabot[bot]@users.noreply.github.com",
    "bot@renovateapp.com",
    "action@github.com",
    "gitlab@localhost",
}

BOT_EMAIL_PATTERNS: list[str] = [
    r".*\[bot\]@.*",
    r".*noreply@.*",
    r".*bot@.*",
    r"^29139614\+.*@users\.noreply\.github\.com$",
]

_compiled_patterns: list[re.Pattern] = [re.compile(p, re.IGNORECASE) for p in BOT_EMAIL_PATTERNS]


# ── Detection ─────────────────────────────────────────────────────────────────

def is_bot(name: str = "", email: str = "") -> bool:
    """Return ``True`` if the name/email belongs to a known bot account."""
    name_stripped = name.strip()
    if name_stripped in BOT_NAMES:
        return True
    # Any name containing [bot] is a bot
    if "[bot]" in name_stripped.lower():
        return True
    email_lower = email.strip().lower()
    if email_lower in BOT_EMAILS:
        return True
    # Check email patterns (both local part and domain)
    if "noreply" in email_lower or "[bot]" in email_lower:
        return True
    for pattern in _compiled_patterns:
        if pattern.match(email_lower):
            return True
    return False


def detect_bots(developers: list[dict]) -> list[dict]:
    """Filter *developers* (list of ``{"name", "email"}`` dicts) to those
    identified as bots."""
    return [
        dev for dev in developers
        if is_bot(name=dev.get("name", ""), email=dev.get("email", ""))
    ]


# ── Config-extensible overrides ──────────────────────────────────────────────

def load_overrides(overrides: Sequence[str]) -> None:
    """Add extra names to :data:`BOT_NAMES` from configuration.

    Parameters
    ----------
    overrides:
        Iterable of bot names (e.g. from ``Config.BOT_OVERRIDES``).
    """
    for name in overrides:
        name = name.strip()
        if name:
            BOT_NAMES.add(name)
            print(f"  Bot override added: {name!r}", flush=True)
