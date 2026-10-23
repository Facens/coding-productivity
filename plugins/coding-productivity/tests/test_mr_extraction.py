"""Merge-request extraction is idempotent, readers see one row per MR, and
GitLab extraction works without --since/--until."""

import sys

import pytest

import analyze
import extract_github
import extract_gitlab
from lib import merge_requests

from .test_duplicate_work import commit, link, mr

CFG = type("Cfg", (), {"REPOS": [], "EXCLUDED_DEVELOPERS": []})()


def rows(storage, table="merge_requests"):
    return storage.query(f"SELECT * FROM {table}")


# ── GitHub ───────────────────────────────────────────────────────────────────

def gh_pr(number, *, state="closed", merged_at="2026-01-02T00:00:00Z", sha="abc",
          updated_at="2026-01-02T00:00:00Z", repo="org/repo", id_base=9000):
    return {"id": id_base + number, "number": number, "state": state, "merged_at": merged_at,
            "closed_at": None, "title": "t", "user": {"login": "u"},
            "created_at": "2026-01-01T00:00:00Z", "updated_at": updated_at,
            "head": {"ref": "feature/x"}, "base": {"ref": "main"},
            "merge_commit_sha": sha, "html_url": f"https://github.com/{repo}/pull/{number}"}


class GhClient:
    def __init__(self, prs, commits=None):
        self.prs = prs
        self.commits = commits or {}  # PR number -> shas; default two per PR
        self.asked = []

    def get_pull_requests(self, repo, state="all"):
        yield from self.prs

    def get_pr_commits(self, repo, number):
        self.asked.append((repo, number))
        shas = self.commits.get(number, [f"c{number}a", f"c{number}b"])
        return [{"sha": s} for s in shas]


def gh_extract(storage, prs, repo="org/repo", **kw):
    return extract_github._extract_pull_requests(
        GhClient(prs), storage, repo, since=None, batch_size=100,
        anon_key=None, merges=None, mapping_path=None, **kw)


def test_github_reextraction_stores_nothing_new(duckdb_storage):
    prs = [gh_pr(1), gh_pr(2, state="open", merged_at=None, sha="testmerge")]
    assert gh_extract(duckdb_storage, prs) == 2
    assert gh_extract(duckdb_storage, prs) == 0  # the old query failed silently: 2 more
    assert len(rows(duckdb_storage)) == 2


def test_github_stores_a_changed_pr_again_and_fills_a_missing_sha_once(duckdb_storage):
    # A row from before 0.14: merged, same updated_at, no sha.
    old = mr(1, merge_sha=None, extracted_at="2026-01-05T00:00:00Z")
    old.update(mr_id="9001", updated_at="2026-01-02T00:00:00Z", project_id="",
               web_url="https://github.com/org/repo/pull/1")
    duckdb_storage.insert_batch("merge_requests", [old])
    assert gh_extract(duckdb_storage, [gh_pr(1)]) == 1          # sha backfill
    assert gh_extract(duckdb_storage, [gh_pr(1)]) == 0          # now current
    assert gh_extract(duckdb_storage, [gh_pr(1, updated_at="2026-01-09T00:00:00Z")]) == 1
    assert len(rows(duckdb_storage)) == 3
    latest = duckdb_storage.query(f"SELECT m.merge_commit_sha FROM {merge_requests.LATEST_MRS} m")
    assert latest == [{"merge_commit_sha": "abc"}]


def test_github_links_carry_the_mr_id_and_are_not_repeated(duckdb_storage):
    gh_extract(duckdb_storage, [gh_pr(1), gh_pr(2)])
    client = GhClient([])
    for _ in range(2):
        extract_github._extract_pr_commit_links(client, duckdb_storage, "org/repo", 100)
    links = duckdb_storage.query("SELECT mr_iid, mr_id, commit_sha FROM mr_commits")
    assert len(links) == 4
    assert {(l["mr_iid"], l["mr_id"]) for l in links} == {("1", "9001"), ("2", "9002")}


def test_github_refetches_links_of_a_pr_stored_again(duckdb_storage):
    # First seen open with one commit; later merged with more. The new commits are
    # linked, the old link is not repeated.
    refreshed = set()
    gh_extract(duckdb_storage, [gh_pr(1, state="open", merged_at=None, sha=None)],
               refreshed=refreshed)
    extract_github._extract_pr_commit_links(
        GhClient([], {1: ["c1a"]}), duckdb_storage, "org/repo", 100, refreshed)
    assert [r["commit_sha"] for r in rows(duckdb_storage, "mr_commits")] == ["c1a"]

    refreshed = set()
    assert gh_extract(duckdb_storage, [gh_pr(1, updated_at="2026-01-09T00:00:00Z")],
                      refreshed=refreshed) == 1
    extract_github._extract_pr_commit_links(
        GhClient([], {1: ["c1a", "c1b", "c1c"]}), duckdb_storage, "org/repo", 100, refreshed)
    shas = sorted(r["commit_sha"] for r in rows(duckdb_storage, "mr_commits"))
    assert shas == ["c1a", "c1b", "c1c"]


def test_github_unchanged_pr_with_links_is_not_refetched(duckdb_storage):
    refreshed = set()
    gh_extract(duckdb_storage, [gh_pr(1)], refreshed=refreshed)
    extract_github._extract_pr_commit_links(
        GhClient([]), duckdb_storage, "org/repo", 100, refreshed)
    refreshed = set()
    assert gh_extract(duckdb_storage, [gh_pr(1)], refreshed=refreshed) == 0
    client = GhClient([])
    extract_github._extract_pr_commit_links(client, duckdb_storage, "org/repo", 100, refreshed)
    assert client.asked == []


def test_github_same_named_repos_do_not_share_prs_or_links(duckdb_storage, capsys):
    gh_extract(duckdb_storage, [gh_pr(1, repo="a/repo", id_base=1000)], repo="a/repo")
    gh_extract(duckdb_storage, [gh_pr(1, repo="b/repo", id_base=2000),
                                gh_pr(2, repo="b/repo", id_base=2000)], repo="b/repo")
    capsys.readouterr()
    gh_extract(duckdb_storage, [], repo="a/repo")
    assert "[a/repo] 1 existing PRs" in capsys.readouterr().out

    client = GhClient([])
    extract_github._extract_pr_commit_links(client, duckdb_storage, "a/repo", 100)
    assert client.asked == [("a/repo", 1)]
    links = duckdb_storage.query("SELECT mr_id, project_id FROM mr_commits")
    assert {(l["mr_id"], l["project_id"]) for l in links} == {("1001", "a/repo")}


def test_github_underscore_in_a_repo_name_is_not_a_wildcard(duckdb_storage, capsys):
    gh_extract(duckdb_storage, [gh_pr(1, repo="a/my-repo", id_base=1000)], repo="a/my-repo")
    capsys.readouterr()
    gh_extract(duckdb_storage, [], repo="a/my_repo")
    assert "[a/my_repo] 0 existing PRs" in capsys.readouterr().out

    client = GhClient([])
    extract_github._extract_pr_commit_links(client, duckdb_storage, "a/my_repo", 100)
    assert client.asked == []


def test_github_query_errors_are_no_longer_swallowed(duckdb_storage):
    duckdb_storage._conn.execute("DROP TABLE mr_commits")
    with pytest.raises(Exception):
        extract_github._extract_pr_commit_links(GhClient([]), duckdb_storage, "org/repo", 100)


# ── GitLab ───────────────────────────────────────────────────────────────────

def gl_mr(iid, *, state="merged", squash="sq", updated_at="2026-01-02T00:00:00Z"):
    return {"id": 100 + iid, "iid": iid, "title": "t", "state": state,
            "author": {"name": "Ann", "email": "ann@example.com"},
            "created_at": "2026-01-01T00:00:00Z", "updated_at": updated_at,
            "merged_at": None, "closed_at": None, "source_branch": "f",
            "target_branch": "main", "merge_commit_sha": None,
            "squash_commit_sha": squash, "web_url": ""}


class GlClient:
    def __init__(self, mrs):
        self.mrs = mrs

    def get_merge_requests(self, project_id, state="all"):
        yield from self.mrs

    def get_mr_commits(self, project_id, iid):
        return [{"id": f"c{iid}a"}, {"id": f"c{iid}b"}]

    def get_branches(self, project_id):
        return [{"name": "main", "commit": {"committed_date": "2026-01-01T00:00:00Z"}}]

    def get_commits(self, project_id, since=None, until=None, ref_name=None):
        return iter([])


def gl_extract(storage, mrs, since="2025-01-01", until="2026-12-31"):
    return extract_gitlab._extract_merge_requests(
        GlClient(mrs), storage, {"id": 42, "name": "api"}, since=since, until=until,
        batch_size=100, anon_key=None, merges=None, excluded=set())


def test_gitlab_reextraction_is_idempotent(duckdb_storage):
    mrs = [gl_mr(1), gl_mr(2, state="opened", squash=None)]
    assert gl_extract(duckdb_storage, mrs) == (2, 4)
    assert gl_extract(duckdb_storage, mrs) == (0, 0)
    assert len(rows(duckdb_storage)) == 2 and len(rows(duckdb_storage, "mr_commits")) == 4
    # A changed MR is stored again, but its links already stored are not.
    assert gl_extract(duckdb_storage, [gl_mr(2, state="merged", squash="sq2",
                                             updated_at="2026-01-05T00:00:00Z")]) == (1, 0)
    assert len(rows(duckdb_storage)) == 3
    latest = {r["mr_iid"]: r for r in duckdb_storage.query(
        f"SELECT m.mr_iid, m.state, m.squash_commit_sha FROM {merge_requests.LATEST_MRS} m")}
    assert latest["2"]["state"] == "merged" and latest["2"]["squash_commit_sha"] == "sq2"


class FailOnce:
    """Storage whose first insert into *table* raises, like a BigQuery stream that
    gave up after its retries."""

    def __init__(self, inner, table):
        self._inner, self._table, self.failed = inner, table, False

    def insert_batch(self, table, records):
        if table == self._table and not self.failed:
            self.failed = True
            raise RuntimeError("streaming insert failed")
        return self._inner.insert_batch(table, records)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_gitlab_a_failed_link_insert_leaves_the_mr_to_be_refetched(duckdb_storage):
    flaky = FailOnce(duckdb_storage, "mr_commits")
    mrs = [gl_mr(1)]
    with pytest.raises(RuntimeError):
        gl_extract(flaky, mrs)
    assert rows(duckdb_storage) == []  # no MR row without its links
    assert gl_extract(flaky, mrs) == (1, 2)
    assert len(rows(duckdb_storage, "mr_commits")) == 2


def test_gitlab_a_failed_link_insert_in_a_full_batch_leaves_the_mr_to_be_refetched(duckdb_storage):
    flaky = FailOnce(duckdb_storage, "mr_commits")
    mrs = [gl_mr(1), gl_mr(2)]
    with pytest.raises(RuntimeError):
        extract_gitlab._extract_merge_requests(
            GlClient(mrs), flaky, {"id": 42, "name": "api"}, since=None, until=None,
            batch_size=1, anon_key=None, merges=None, excluded=set())
    assert rows(duckdb_storage) == []
    assert gl_extract(flaky, mrs) == (2, 4)


def test_gitlab_extraction_works_without_since_and_until(duckdb_storage):
    # --full with no dates used to compare a str with None.
    assert gl_extract(duckdb_storage, [gl_mr(1)], since=None, until=None) == (1, 2)
    assert gl_extract(duckdb_storage, [gl_mr(2)], since="2025-01-01", until=None) == (1, 2)
    extract_gitlab._extract_commits(
        GlClient([]), duckdb_storage, {"id": 42, "name": "api", "path_with_namespace": "g/api"},
        since=None, until=None, batch_size=100, existing_shas=set(), anon_key=None,
        merges=None, excluded=set())


def test_gitlab_cli_full_without_dates_reaches_run(tmp_dir, monkeypatch):
    cfg = tmp_dir / ".coding-productivity.env"
    cfg.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={tmp_dir / 't.duckdb'}\nPLATFORM=gitlab\n"
                   "GITLAB_TOKEN=x\nGITLAB_URL=https://gitlab.example.com\nREPOS=g/api\n")
    cfg.chmod(0o600)
    seen = {}
    monkeypatch.setattr(extract_gitlab, "run", lambda config, **kw: seen.update(kw))
    monkeypatch.setattr(sys, "argv", ["extract_gitlab", "--config", str(cfg), "--full"])
    extract_gitlab.main()
    assert seen["since"] is None and seen["until"] is None


# ── Readers see one row per MR ───────────────────────────────────────────────

def test_merge_velocity_and_code_efficiency_read_one_row_per_mr(duckdb_storage):
    st = duckdb_storage
    created = "2026-01-05T00:00:00Z"
    versions = [
        dict(mr(1, state="opened", extracted_at="2026-02-01T00:00:00Z"), mr_id="a", created_at=created),
        dict(mr(1, state="merged", extracted_at="2026-02-02T00:00:00Z"), mr_id="a",
             created_at=created, merged_at="2026-01-06T00:00:00Z"),
        dict(mr(1, state="merged", extracted_at="2026-02-03T00:00:00Z"), mr_id="a",
             created_at=created, merged_at="2026-01-06T00:00:00Z"),
        dict(mr(2, state="opened", extracted_at="2026-02-01T00:00:00Z"), mr_id="b", created_at=created),
    ]
    st.insert_batch("merge_requests", versions)
    st.insert_batch("commits", [commit("c1", adds=10, date="2026-01-10T10:00:00Z")])
    st.insert_batch("mr_commits", [link(1, "c1")])

    velocity = analyze.merge_velocity(st, CFG, "2026-01-01", "2026-02-01")[0]
    assert (velocity["total_mrs"], velocity["merged_count"]) == (2, 1)

    eff = analyze.code_efficiency(st, CFG, "2026-01-01", "2026-02-01")[0]
    assert (eff["total_additions"], eff["merged_additions"]) == (10, 10)  # not 30


def test_needs_refresh_rules():
    base = {"state": "merged", "updated_at": "2026-01-02T00:00:00Z",
            "merge_commit_sha": "x", "squash_commit_sha": None}
    rec = dict(base)
    assert not merge_requests.needs_refresh(base, rec)
    assert merge_requests.needs_refresh(None, rec)
    assert merge_requests.needs_refresh(base, dict(rec, state="closed"))
    assert merge_requests.needs_refresh(base, dict(rec, updated_at="2026-01-03T00:00:00Z"))
    # No sha anywhere (a fast-forward merge): nothing new to store.
    bare = dict(base, merge_commit_sha=None)
    assert not merge_requests.needs_refresh(bare, dict(rec, merge_commit_sha=None))
    assert merge_requests.needs_refresh(bare, rec)
