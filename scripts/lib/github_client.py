"""
GitHub REST API client with rate limiting, retry, and session recreation.

Wraps ``requests.Session`` and integrates with :class:`RateLimiter` for
header-aware throttling.  On connection errors the session is recreated
to discard stale TCP connections (see institutional learning on VPN drops).
"""

from __future__ import annotations

import time
from typing import Generator, Optional

import requests

from .rate_limiter import RateLimiter


# GitHub allows 5 000 authenticated requests per hour.
_REQUESTS_PER_HOUR = 5000
_FIXED_DELAY = 3600 / _REQUESTS_PER_HOUR  # ~0.72 s
_MAX_RETRIES = 5


class GitHubClient:
    """GitHub REST API client."""

    def __init__(self, token: str):
        self.base_url = "https://api.github.com"
        self._token = token
        self._session = self._make_session()
        self._limiter = RateLimiter(
            fixed_delay=_FIXED_DELAY,
            platform="github",
        )
        self.request_count = 0

    # ── Session management ────────────────────────────────────────────────

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        return s

    def _recreate_session(self) -> None:
        """Discard the connection pool and create a fresh session."""
        try:
            self._session.close()
        except Exception:
            pass
        self._session = self._make_session()

    # ── Low-level request ─────────────────────────────────────────────────

    def _request(self, url: str, params: dict | None = None) -> Optional[requests.Response]:
        """Issue a GET with retry, rate limiting, and session recreation."""
        if not url.startswith("http"):
            url = f"{self.base_url}{url}"

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

                if resp.status_code == 409:
                    # Empty repository
                    return None

                if resp.status_code == 429:
                    wait = self._limiter.handle_429(resp)
                    print(f"  Rate limited (429), waiting {wait:.0f}s...", flush=True)
                    time.sleep(wait)
                    continue

                if resp.status_code == 403:
                    reset_hdr = resp.headers.get("X-RateLimit-Reset")
                    if reset_hdr and float(reset_hdr) > time.time():
                        wait = float(reset_hdr) - time.time() + 5
                        print(
                            f"  Rate limit exceeded (403), waiting {wait:.0f}s...",
                            flush=True,
                        )
                        time.sleep(wait)
                        continue
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

    def _paginate(self, url: str, params: dict | None = None) -> Generator[dict, None, None]:
        """Follow GitHub Link-header pagination and yield individual items."""
        if params is None:
            params = {}
        params.setdefault("per_page", 100)

        full_url = url if url.startswith("http") else f"{self.base_url}{url}"
        current_params: dict | None = params

        while full_url:
            resp = self._request(full_url, current_params)
            if resp is None:
                break

            data = resp.json()
            if isinstance(data, list):
                yield from data
                if len(data) < params.get("per_page", 100):
                    break
            else:
                yield data
                break

            # Follow Link: <url>; rel="next"
            next_url = self._parse_next_link(resp)
            if next_url:
                full_url = next_url
                current_params = None  # params are baked into the Link URL
            else:
                break

    @staticmethod
    def _parse_next_link(resp: requests.Response) -> Optional[str]:
        link_header = resp.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                return url
        return None

    # ── Public methods ────────────────────────────────────────────────────

    def list_repos(self, org: str) -> list[dict]:
        """Return all non-archived repositories for *org*."""
        print(f"Fetching repos for org {org}...", flush=True)
        repos = []
        for repo in self._paginate(f"/orgs/{org}/repos", {"type": "all", "sort": "updated"}):
            if repo.get("archived"):
                continue
            repos.append({
                "id": repo["id"],
                "name": repo["name"],
                "full_name": repo["full_name"],
                "default_branch": repo.get("default_branch", "main"),
                "updated_at": repo.get("updated_at"),
            })
        print(f"  Found {len(repos)} repos", flush=True)
        return repos

    def get_commits(
        self,
        repo: str,
        since: str | None = None,
        until: str | None = None,
    ) -> Generator[dict, None, None]:
        """Yield commits for *repo* (``owner/name``), optionally date-filtered."""
        params: dict = {}
        if since:
            params["since"] = f"{since}T00:00:00Z"
        if until:
            params["until"] = f"{until}T23:59:59Z"

        for commit in self._paginate(f"/repos/{repo}/commits", params):
            yield commit

    def get_commit_detail(self, repo: str, sha: str) -> Optional[dict]:
        """Return full commit detail including file patches."""
        resp = self._request(f"/repos/{repo}/commits/{sha}")
        if resp is None:
            return None
        return resp.json()

    def get_pull_requests(
        self,
        repo: str,
        state: str = "all",
    ) -> Generator[dict, None, None]:
        """Yield pull requests for *repo*."""
        params = {"state": state, "sort": "updated", "direction": "desc"}
        for pr in self._paginate(f"/repos/{repo}/pulls", params):
            yield pr

    def get_pr_commits(self, repo: str, pr_number: int) -> list[dict]:
        """Return all commits belonging to a pull request."""
        commits = []
        for commit in self._paginate(f"/repos/{repo}/pulls/{pr_number}/commits"):
            commits.append(commit)
        return commits
