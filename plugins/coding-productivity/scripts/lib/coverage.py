"""
Repo coverage check for extraction.

When ``REPOS`` is an explicit list, new repositories can appear in the same
organization (GitHub) or group (GitLab) without anyone adding them, and a
configured repository can be archived. This module compares ``REPOS`` with one
listing call per organization or group and returns warnings. It never changes
what is extracted.
"""

from __future__ import annotations

from typing import Callable, Iterable

_SHOWN = 20  # repositories named in one warning


def _warn_missing(scope: str, names: list[str]) -> str:
    shown = ", ".join(sorted(names)[:_SHOWN])
    more = f" and {len(names) - _SHOWN} more" if len(names) > _SHOWN else ""
    return (
        f"Warning: {len(names)} active repo(s) in {scope} are not in REPOS: "
        f"{shown}{more}. Add them to REPOS if they should be measured."
    )


def _warn_archived(names: list[str]) -> str:
    return (
        f"Warning: {len(names)} configured repo(s) are archived: "
        f"{', '.join(sorted(names))}. They get no new commits; remove them from "
        "REPOS if they are no longer relevant."
    )


def _safe(check: Callable[[], list[str]]) -> list[str]:
    """Run *check*; a failing listing must never stop an extraction."""
    try:
        return check()
    except Exception as exc:  # noqa: BLE001 - warning only, whatever went wrong
        return [f"Warning: repo coverage check skipped ({type(exc).__name__}: {exc})."]


def github_coverage(client, repos: Iterable[str]) -> list[str]:
    """Warnings for the organizations that own *repos* (``owner/name``)."""
    configured = {r.strip().lower() for r in repos if "/" in r}
    owners = sorted({r.split("/", 1)[0] for r in configured})

    def check() -> list[str]:
        warnings: list[str] = []
        archived: list[str] = []
        for owner in owners:
            missing = []
            for repo in client.list_repos(owner, include_archived=True):
                name = repo["full_name"]
                if repo.get("archived"):
                    if name.lower() in configured:
                        archived.append(name)
                elif name.lower() not in configured:
                    missing.append(name)
            if missing:
                warnings.append(_warn_missing(f"GitHub org {owner}", missing))
        if archived:
            warnings.append(_warn_archived(archived))
        return warnings

    return _safe(check)


def _parent(path: str) -> str | None:
    return path.rsplit("/", 1)[0] if "/" in path else None


def gitlab_coverage(client, projects: Iterable[dict]) -> list[str]:
    """Warnings for the groups that directly hold the resolved *projects*.

    *projects* are the dicts the extractor resolved (``id``,
    ``path_with_namespace``, optionally ``archived``). Nested groups are
    covered by their ancestor's listing, which includes subgroups.
    """
    projects = list(projects)
    configured_ids = {str(p["id"]) for p in projects}
    groups = sorted({g for p in projects if (g := _parent(p["path_with_namespace"]))})
    groups = [g for g in groups
              if not any(g.startswith(other + "/") for other in groups)]

    def check() -> list[str]:
        warnings: list[str] = []
        archived = [p["path_with_namespace"] for p in projects if p.get("archived")]
        for group in groups:
            missing = []
            for project in client.list_projects(group):
                if project.get("archived"):
                    if (str(project["id"]) in configured_ids
                            and project["path_with_namespace"] not in archived):
                        archived.append(project["path_with_namespace"])
                elif str(project["id"]) not in configured_ids:
                    missing.append(project["path_with_namespace"])
            if missing:
                warnings.append(_warn_missing(f"GitLab group {group}", missing))
        if archived:
            warnings.append(_warn_archived(archived))
        return warnings

    return _safe(check)
