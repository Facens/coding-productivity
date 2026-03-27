"""
GitLab REST API client with rate limiting, retry, and session recreation.

Wraps ``requests.Session`` and integrates with :class:`RateLimiter` for
header-aware throttling.  On connection errors the session is recreated
to discard stale TCP connections (see institutional learning on VPN drops).
"""

from __future__ import annotations

import time
from typing import Generator, Optional
from urllib.parse import quote as urlquote

import requests

from .rate_limiter import RateLimiter


# GitLab self-hosted typically allows ~2 000 requests/minute; we target 1 800.
_REQUESTS_PER_MINUTE = 1800
_FIXED_DELAY = 60 / _REQUESTS_PER_MINUTE  # ~0.033 s
_MAX_RETRIES = 5


class GitLabClient:
    """GitLab REST API client."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://gitlab.com",
        ca_bundle: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._ca_bundle = ca_bundle
        self._session = self._make_session()
        self._limiter = RateLimiter(
            fixed_delay=_FIXED_DELAY,
            platform="gitlab",
        )
        self.request_count = 0

    # ── Session management ────────────────────────────────────────────────

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        s.headers["PRIVATE-TOKEN"] = self._token
        # TLS: always verify.  Use a custom CA bundle when provided.
        if self._ca_bundle:
            s.verify = self._ca_bundle
        else:
            s.verify = True
        return s

    def _recreate_session(self) -> None:
        """Discard the connection pool and create a fresh session."""
        try:
            self._session.close()
        except Exception:
            pass
        self._session = self._make_session()

    # ── Low-level request ─────────────────────────────────────────────────

    def _request(self, endpoint: str, params: dict | None = None) -> Optional[requests.Response]:
        """Issue a GET with retry, rate limiting, and session recreation."""
        url = f"{self.base_url}/api/v4{endpoint}"

        for attempt in range(_MAX_RETRIES):
            self._limiter.wait()
            try:
                resp = self._session.get(url, params=params, timeout=30)
                self.request_count += 1
                self._limiter.update_from_response(resp)

                if resp.status_code == 200:
                    return resp

                if resp.status_code == 404:
                    return None

                if resp.status_code == 429:
                    wait = self._limiter.handle_429(resp)
                    print(f"  Rate limited (429), waiting {wait:.0f}s...", flush=True)
                    time.sleep(wait)
                    continue

                if resp.status_code == 403:
                    print(f"  403 Forbidden: {url}", flush=True)
                    return None

                # Other errors -- exponential backoff
                print(
                    f"  HTTP {resp.status_code}: {url}",
                    flush=True,
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                wait = 2 ** attempt * 5
                print(
                    f"  Connection error, recreating session, retrying in {wait}s... ({exc})",
                    flush=True,
                )
                time.sleep(wait)
                self._recreate_session()
                continue

        return None

    # ── Pagination helper ─────────────────────────────────────────────────

    def _paginate(self, endpoint: str, params: dict | None = None) -> Generator[dict, None, None]:
        """Iterate through GitLab paginated results, yielding individual items."""
        if params is None:
            params = {}
        params.setdefault("per_page", 100)
        page = 1

        while True:
            params["page"] = page
            resp = self._request(endpoint, params)
            if resp is None:
                break

            data = resp.json()
            if not data:
                break

            for item in data:
                yield item

            if len(data) < params.get("per_page", 100):
                break

            # Also respect Link header if present
            next_page = resp.headers.get("X-Next-Page")
            if next_page:
                try:
                    page = int(next_page)
                except (ValueError, TypeError):
                    break
            else:
                page += 1

    # ── Public methods ────────────────────────────────────────────────────

    def list_projects(self, group: str | None = None) -> list[dict]:
        """Return all accessible projects, optionally filtered by *group*.

        *group* can be a group ID (int-like string) or a path (e.g.
        ``my-org/sub-group``).  Paths are URL-encoded automatically.
        """
        if group is not None:
            encoded = urlquote(str(group), safe="")
            endpoint = f"/groups/{encoded}/projects"
            params = {"include_subgroups": "true"}
            print(f"Fetching projects for group {group}...", flush=True)
        else:
            endpoint = "/projects"
            params = {"membership": "true"}
            print("Fetching accessible projects...", flush=True)

        projects = []
        for project in self._paginate(endpoint, params):
            if project.get("empty_repo"):
                continue
            projects.append({
                "id": project["id"],
                "name": project["name"],
                "path_with_namespace": project["path_with_namespace"],
                "default_branch": project.get("default_branch", "main"),
                "last_activity_at": project.get("last_activity_at"),
            })

        print(f"  Found {len(projects)} projects", flush=True)
        return projects

    def get_commits(
        self,
        project_id: int,
        since: str | None = None,
        until: str | None = None,
        ref_name: str | None = None,
    ) -> Generator[dict, None, None]:
        """Yield commits for a project, optionally date-filtered."""
        params: dict = {"with_stats": "true"}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if ref_name:
            params["ref_name"] = ref_name

        for commit in self._paginate(f"/projects/{project_id}/repository/commits", params):
            yield commit

    def get_commit_diff(self, project_id: int, sha: str) -> list[dict]:
        """Return the file-level diff list for a single commit."""
        resp = self._request(f"/projects/{project_id}/repository/commits/{sha}/diff")
        if resp is None:
            return []
        return resp.json()

    def get_merge_requests(
        self,
        project_id: int,
        state: str = "all",
    ) -> Generator[dict, None, None]:
        """Yield merge requests for a project."""
        params = {"state": state, "order_by": "updated_at", "sort": "desc"}
        for mr in self._paginate(f"/projects/{project_id}/merge_requests", params):
            yield mr

    def get_mr_commits(self, project_id: int, mr_iid: int) -> list[dict]:
        """Return all commits belonging to a merge request."""
        commits = []
        for commit in self._paginate(
            f"/projects/{project_id}/merge_requests/{mr_iid}/commits"
        ):
            commits.append(commit)
        return commits

    def get_branches(self, project_id: int) -> list[dict]:
        """Return all branches for a project."""
        branches = []
        for branch in self._paginate(f"/projects/{project_id}/repository/branches"):
            branches.append(branch)
        return branches
