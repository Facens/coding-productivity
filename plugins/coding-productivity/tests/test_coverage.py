"""Repo coverage check at extract time: warnings only, one listing per org/group."""

from types import SimpleNamespace as NS

import extract_github
import extract_gitlab
from lib import coverage
from lib.github_client import GitHubClient
from lib.gitlab_client import GitLabClient


# ── Fakes ────────────────────────────────────────────────────────────────────

def gh_repo(full_name, archived=False):
    return {"id": 1, "name": full_name.split("/")[1], "full_name": full_name,
            "archived": archived}


class FakeGitHub:
    """list_repos only: org -> repos. Records every call."""

    def __init__(self, orgs):
        self.orgs = orgs
        self.calls = []

    def list_repos(self, org, include_archived=False):
        self.calls.append((org, include_archived))
        return [r for r in self.orgs.get(org, []) if include_archived or not r["archived"]]


def gl_project(pid, path, archived=False):
    return {"id": pid, "name": path.rsplit("/", 1)[1], "path_with_namespace": path,
            "archived": archived}


class FakeGitLab:
    def __init__(self, groups):
        self.groups = groups
        self.calls = []

    def list_projects(self, group=None):
        self.calls.append(group)
        return self.groups.get(group, [])


# ── GitHub ───────────────────────────────────────────────────────────────────

def test_github_warns_about_active_repos_not_in_repos():
    client = FakeGitHub({"acme": [gh_repo("acme/api"), gh_repo("acme/web"),
                                  gh_repo("acme/new-thing"), gh_repo("acme/old", archived=True)]})
    warnings = coverage.github_coverage(client, ["acme/api", "acme/web"])
    assert len(warnings) == 1
    assert "1 active repo(s) in GitHub org acme are not in REPOS: acme/new-thing" in warnings[0]
    assert "acme/old" not in warnings[0]          # archived and not configured: not news


def test_github_warns_about_configured_repos_that_are_archived():
    client = FakeGitHub({"acme": [gh_repo("acme/api"), gh_repo("acme/old", archived=True)]})
    warnings = coverage.github_coverage(client, ["acme/api", "ACME/Old"])
    assert warnings == ["Warning: 1 configured repo(s) are archived: acme/old. They get no "
                        "new commits; remove them from REPOS if they are no longer relevant."]


def test_github_complete_config_is_silent_and_matching_ignores_case():
    client = FakeGitHub({"acme": [gh_repo("acme/API")]})
    assert coverage.github_coverage(client, ["acme/api"]) == []


def test_github_one_listing_call_per_org():
    client = FakeGitHub({"acme": [gh_repo("acme/a"), gh_repo("acme/b")],
                         "other": [gh_repo("other/x")]})
    warnings = coverage.github_coverage(client, ["acme/a", "acme/b", "other/x"])
    assert warnings == []
    assert sorted(client.calls) == [("acme", True), ("other", True)]


def test_github_long_lists_are_cut_and_counted():
    repos = [gh_repo(f"acme/r{i:02d}") for i in range(25)]
    warning = coverage.github_coverage(FakeGitHub({"acme": repos}), ["acme/x"])[0]
    assert "25 active repo(s)" in warning and "and 5 more" in warning
    assert "acme/r19" in warning and "acme/r20" not in warning


def test_github_a_failing_listing_is_a_warning_not_an_error():
    class Broken:
        def list_repos(self, org, include_archived=False):
            raise RuntimeError("boom")

    warnings = coverage.github_coverage(Broken(), ["acme/a"])
    assert len(warnings) == 1 and "skipped" in warnings[0] and "boom" in warnings[0]


def test_github_client_lists_archived_repos_only_on_request(monkeypatch):
    client = GitHubClient("tok")
    api = [{"id": 1, "name": "a", "full_name": "o/a", "archived": False},
           {"id": 2, "name": "b", "full_name": "o/b", "archived": True}]
    monkeypatch.setattr(client, "_paginate", lambda url, params=None: iter(api))
    assert [r["full_name"] for r in client.list_repos("o")] == ["o/a"]
    both = client.list_repos("o", include_archived=True)
    assert [(r["full_name"], r["archived"]) for r in both] == [("o/a", False), ("o/b", True)]


def test_github_main_prints_the_warning_and_still_extracts(tmp_dir, monkeypatch, capsys):
    cfg = tmp_dir / ".coding-productivity.env"
    cfg.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={tmp_dir / 't.duckdb'}\nPLATFORM=github\n"
                   "GITHUB_TOKEN=x\nREPOS=acme/api\n")
    cfg.chmod(0o600)
    fake = FakeGitHub({"acme": [gh_repo("acme/api"), gh_repo("acme/new")]})
    fake.request_count = 0
    seen = []
    monkeypatch.setattr(extract_github, "GitHubClient", lambda token: fake)
    monkeypatch.setattr(extract_github, "_process_repo",
                        lambda client, storage, repo, **kw: seen.append(repo) or {})
    extract_github.main(["--config", str(cfg), "--full"])
    out = capsys.readouterr().out
    assert "acme/new" in out and "not in REPOS" in out
    assert seen == ["acme/api"]                    # behaviour unchanged


def test_github_single_repo_mode_skips_the_check(tmp_dir, monkeypatch):
    cfg = tmp_dir / ".coding-productivity.env"
    cfg.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={tmp_dir / 't.duckdb'}\nPLATFORM=github\n"
                   "GITHUB_TOKEN=x\nREPOS=acme/api\n")
    cfg.chmod(0o600)
    fake = FakeGitHub({"acme": [gh_repo("acme/new")]})
    fake.request_count = 0
    monkeypatch.setattr(extract_github, "GitHubClient", lambda token: fake)
    monkeypatch.setattr(extract_github, "_process_repo", lambda *a, **kw: {})
    extract_github.main(["--config", str(cfg), "--full", "--repo", "acme/api"])
    assert fake.calls == []


# ── GitLab ───────────────────────────────────────────────────────────────────

def test_gitlab_warns_about_active_projects_not_configured():
    client = FakeGitLab({"org/team": [gl_project(1, "org/team/api"), gl_project(2, "org/team/new"),
                                      gl_project(3, "org/team/dead", archived=True)]})
    warnings = coverage.gitlab_coverage(client, [gl_project(1, "org/team/api")])
    assert len(warnings) == 1
    assert "GitLab group org/team are not in REPOS: org/team/new" in warnings[0]
    assert client.calls == ["org/team"]


def test_gitlab_warns_about_configured_archived_projects():
    client = FakeGitLab({"org/team": [gl_project(1, "org/team/api"),
                                      gl_project(3, "org/team/dead", archived=True)]})
    projects = [gl_project(1, "org/team/api"), gl_project(3, "org/team/dead")]
    warnings = coverage.gitlab_coverage(client, projects)
    assert len(warnings) == 1 and "org/team/dead" in warnings[0] and "archived" in warnings[0]


def test_gitlab_uses_the_archived_flag_of_a_resolved_project():
    # A project resolved by id whose group listing does not mention it.
    client = FakeGitLab({"org/team": [gl_project(1, "org/team/api")]})
    projects = [gl_project(1, "org/team/api"), gl_project(9, "org/team/old", archived=True)]
    warnings = coverage.gitlab_coverage(client, projects)
    assert len(warnings) == 1 and "org/team/old" in warnings[0]


def test_gitlab_nested_groups_are_covered_by_the_ancestor_listing():
    client = FakeGitLab({"org": [gl_project(1, "org/a/x"), gl_project(2, "org/b/y"),
                                 gl_project(3, "org/b/z")]})
    projects = [gl_project(1, "org/a/x"), gl_project(2, "org/b/y")]
    # org itself holds no configured project directly, so each parent is listed once,
    # and a parent that sits below another listed group is dropped.
    projects.append(gl_project(4, "org/top"))
    warnings = coverage.gitlab_coverage(client, projects)
    assert client.calls == ["org"]
    assert "org/b/z" in warnings[0]


def test_gitlab_a_failing_listing_is_a_warning_not_an_error():
    class Broken:
        def list_projects(self, group=None):
            raise RuntimeError("403")

    warnings = coverage.gitlab_coverage(Broken(), [gl_project(1, "org/team/api")])
    assert len(warnings) == 1 and "skipped" in warnings[0]


def test_gitlab_client_flags_archived_projects(monkeypatch):
    client = GitLabClient(token="t", base_url="https://gitlab.example.com")
    api = [{"id": 1, "name": "a", "path_with_namespace": "g/a", "archived": True},
           {"id": 2, "name": "b", "path_with_namespace": "g/b"},
           {"id": 3, "name": "c", "path_with_namespace": "g/c", "empty_repo": True}]
    monkeypatch.setattr(client, "_paginate", lambda endpoint, params=None: iter(api))
    listed = client.list_projects("g")
    assert [(p["id"], p["archived"]) for p in listed] == [(1, True), (2, False)]


def test_gitlab_resolved_project_carries_archived(monkeypatch):
    data = {"id": 5, "name": "a", "path_with_namespace": "g/a", "archived": True}
    client = NS(_request=lambda endpoint: NS(json=lambda: data))
    assert extract_gitlab._resolve_project_id(client, "g/a")["archived"] is True


def test_gitlab_run_prints_the_warning_and_keeps_extracting(tmp_dir, monkeypatch, capsys):
    cfg_path = tmp_dir / ".coding-productivity.env"
    cfg_path.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={tmp_dir / 't.duckdb'}\nPLATFORM=gitlab\n"
                        "GITLAB_TOKEN=x\nGITLAB_URL=https://gitlab.example.com\nREPOS=org/team/api\n")
    cfg_path.chmod(0o600)

    class Client(FakeGitLab):
        request_count = 0

        def __init__(self, **kw):
            super().__init__({"org/team": [gl_project(1, "org/team/api"),
                                           gl_project(2, "org/team/new")]})

        def _request(self, endpoint):
            data = {"id": 1, "name": "api", "path_with_namespace": "org/team/api"}
            return NS(json=lambda: data)

    stored = []
    monkeypatch.setattr(extract_gitlab, "GitLabClient", Client)
    monkeypatch.setattr(extract_gitlab, "checkpoint",
                        NS(load=lambda slug: None, save=lambda *a: None, clear=lambda *a: None))
    monkeypatch.setattr(extract_gitlab, "_extract_commits",
                        lambda client, storage, project, **kw: stored.append(project["id"]) or (0, 0, 0))
    monkeypatch.setattr(extract_gitlab, "_extract_merge_requests",
                        lambda *a, **kw: (0, 0))
    extract_gitlab.run(extract_gitlab.Config(cfg_path), single_project=None,
                       since=None, until=None, batch_size=10)
    out = capsys.readouterr().out
    assert "org/team/new" in out and "not in REPOS" in out
    assert stored == [1]
