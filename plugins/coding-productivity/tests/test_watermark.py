"""Incremental extraction starts per repo, so an interrupted run leaves no gap."""

from types import SimpleNamespace as NS
from unittest.mock import patch

import extract_github
import extract_gitlab
from lib import checkpoint, watermark
from lib.storage import DuckDBStorage


def _commit(sha, path, pid, date):
    return {"commit_sha": sha, "project_id": pid, "project_name": path.split("/")[-1],
            "project_path": path, "author_name": "a", "author_email": "a@x",
            "committer_name": "a", "committer_email": "a@x", "authored_date": date,
            "committed_date": date, "created_at": date, "branch_name": "main",
            "title": "t", "message": "t", "additions": 1, "deletions": 0,
            "total_changes": 1, "parent_ids": "", "web_url": "",
            "extracted_at": "2026-10-31T00:00:00Z"}


def _store(tmp_dir):
    """Repo a ran last (Oct 30), repo b before an interruption (Oct 1)."""
    db = tmp_dir / "t.duckdb"
    st = DuckDBStorage(str(db))
    st.create_tables()
    st.insert_batch("commits", [
        _commit("a1", "acme/a", "1", "2026-10-30T17:34:01Z"),
        _commit("b1", "acme/b", "2", "2026-10-01T21:37:19Z"),
    ])
    st.close()
    return db


# ── resolve ──────────────────────────────────────────────────────────────────

def test_resolve_uses_the_repo_latest_commit():
    assert watermark.resolve("2026-10-01T00:00:00Z", "2026-10-30T00:00:00Z", None)[0] \
        == "2026-10-01T00:00:00Z"


def test_resolve_falls_back_to_the_store_for_a_repo_with_no_commits():
    since, why = watermark.resolve(None, "2026-10-30T00:00:00Z", None)
    assert since == "2026-10-30T00:00:00Z" and "--full" in why


def test_resolve_resumes_the_window_of_an_unfinished_run():
    ckpt = {"phase": "commits", "run_since": "2026-09-30T00:00:00Z"}
    assert watermark.resolve("2026-10-30T00:00:00Z", None, ckpt)[0] == "2026-09-30T00:00:00Z"
    # An unfinished full run resumes as a full run.
    assert watermark.resolve("2026-10-30T00:00:00Z", None, {"run_since": ""})[0] is None


def test_resolve_warns_about_a_checkpoint_without_a_window():
    since, why = watermark.resolve("2026-10-30T00:00:00Z", None, {"phase": "commits"})
    assert since == "2026-10-30T00:00:00Z" and "--since" in why


def test_latest_commit_filters_and_formats(tmp_dir):
    st = DuckDBStorage(str(_store(tmp_dir)))
    assert watermark.latest_commit(st) == "2026-10-30T17:34:01Z"
    assert watermark.latest_commit(st, "project_id = $p", {"p": "2"}) == "2026-10-01T21:37:19Z"
    assert watermark.latest_commit(st, "project_id = $p", {"p": "9"}) is None
    st.close()


# ── checkpoint keeps the window ──────────────────────────────────────────────

def test_checkpoint_save_keeps_run_since_across_phases(tmp_dir):
    with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir):
        checkpoint.save("acme_b", {"phase": "start", "run_since": "2026-10-01T00:00:00Z"})
        checkpoint.save("acme_b", {"phase": "commits", "last_sha": "x"})
        assert checkpoint.load("acme_b", quiet=True)["run_since"] == "2026-10-01T00:00:00Z"


# ── GitHub ───────────────────────────────────────────────────────────────────

def _github_main(tmp_dir, monkeypatch, repos, *extra):
    db = _store(tmp_dir)
    cfg = tmp_dir / ".coding-productivity.env"
    cfg.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={db}\nPLATFORM=github\n"
                   f"GITHUB_TOKEN=x\nREPOS={repos}\n")
    cfg.chmod(0o600)
    seen = {}
    monkeypatch.setattr(extract_github, "GitHubClient",
                        lambda token: NS(list_repos=lambda *a, **kw: [], request_count=0))
    monkeypatch.setattr(extract_github, "_process_repo",
                        lambda client, storage, repo, since, **kw: seen.update({repo: since}) or {})
    extract_github.main(["--config", str(cfg), *extra])
    return seen


def test_github_each_repo_starts_from_its_own_latest_commit(tmp_dir, monkeypatch):
    with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir / "ckpt"):
        seen = _github_main(tmp_dir, monkeypatch, "acme/a,Acme/B,acme/new")
    assert seen == {
        "acme/a": "2026-10-30T17:34:01Z",
        "Acme/B": "2026-10-01T21:37:19Z",        # not Oct 30: the gap this fixes
        "acme/new": "2026-10-30T17:34:01Z",      # no commits: store fallback
    }


def test_github_resumes_an_unfinished_repo_from_its_window(tmp_dir, monkeypatch):
    with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir / "ckpt"):
        checkpoint.save("acme_a", {"phase": "commits", "run_since": "2026-10-01T21:37:19Z"})
        seen = _github_main(tmp_dir, monkeypatch, "acme/a")
    assert seen == {"acme/a": "2026-10-01T21:37:19Z"}


def test_github_explicit_since_and_full_are_unchanged(tmp_dir, monkeypatch):
    with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir / "ckpt"):
        assert _github_main(tmp_dir, monkeypatch, "acme/a,acme/b", "--since", "2026-09-01") \
            == {"acme/a": "2026-09-01", "acme/b": "2026-09-01"}
    other = tmp_dir / "full"
    other.mkdir()
    with patch.object(checkpoint, "_CHECKPOINT_DIR", other / "ckpt"):
        assert _github_main(other, monkeypatch, "acme/a", "--full") == {"acme/a": None}


def test_github_process_repo_records_the_window_and_clears_it(tmp_dir, monkeypatch):
    for phase in ("_extract_commits", "_extract_diffs", "_extract_pull_requests",
                  "_extract_pr_commit_links"):
        monkeypatch.setattr(extract_github, phase, lambda *a, **kw: 0)
    with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir):
        def boom(*a, **kw):
            raise RuntimeError("rate limited")
        monkeypatch.setattr(extract_github, "_extract_diffs", boom)
        extract_github._process_repo(None, None, "acme/b", since="2026-10-01T21:37:19Z",
                                     until=None, batch_size=10, anon_key=None, merges=None,
                                     mapping_path=None)
        assert checkpoint.load("acme_b", quiet=True)["run_since"] == "2026-10-01T21:37:19Z"

        monkeypatch.setattr(extract_github, "_extract_diffs", lambda *a, **kw: 0)
        extract_github._process_repo(None, None, "acme/b", since="2026-10-01T21:37:19Z",
                                     until=None, batch_size=10, anon_key=None, merges=None,
                                     mapping_path=None)
        assert checkpoint.load("acme_b", quiet=True) is None


# ── GitLab ───────────────────────────────────────────────────────────────────

def test_gitlab_each_project_starts_from_its_own_latest_commit(tmp_dir, monkeypatch):
    db = _store(tmp_dir)
    cfg = tmp_dir / ".coding-productivity.env"
    cfg.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={db}\nPLATFORM=gitlab\n"
                   "GITLAB_TOKEN=x\nGITLAB_URL=https://gitlab.example.com\nREPOS=1,2,3\n")
    cfg.chmod(0o600)
    projects = {str(i): {"id": i, "name": f"p{i}", "path_with_namespace": f"g/p{i}"}
                for i in (1, 2, 3)}
    monkeypatch.setattr(extract_gitlab, "GitLabClient",
                        lambda **kw: NS(request_count=0))
    monkeypatch.setattr(extract_gitlab, "_resolve_project_id", lambda c, ident: projects[ident])
    monkeypatch.setattr(extract_gitlab.coverage, "gitlab_coverage", lambda *a: [])
    seen_commits, seen_mrs = {}, {}
    monkeypatch.setattr(extract_gitlab, "_extract_commits",
                        lambda client, storage, project, since, **kw:
                        seen_commits.update({project["id"]: since}) or (0, 0, 0))
    monkeypatch.setattr(extract_gitlab, "_extract_merge_requests",
                        lambda client, storage, project, since, **kw:
                        seen_mrs.update({project["id"]: since}) or (0, 0))
    with patch.object(checkpoint, "_CHECKPOINT_DIR", tmp_dir / "ckpt"):
        extract_gitlab.run(extract_gitlab.Config(cfg), single_project=None, since=None,
                           until=None, batch_size=10, store_latest="2026-10-30T17:34:01Z")
    expected = {1: "2026-10-30T17:34:01Z", 2: "2026-10-01T21:37:19Z", 3: "2026-10-30T17:34:01Z"}
    assert seen_commits == expected and seen_mrs == expected
