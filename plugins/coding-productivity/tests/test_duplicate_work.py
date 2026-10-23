"""Duplicate-work detection: the five rules on small synthetic repos."""

import pytest

from lib import duplicate_work as dw

# ── Builders ─────────────────────────────────────────────────────────────────

_N = 0


def commit(sha, *, project="org/repo", email="dev@example.com", title="work",
           date="2026-01-10T10:00:00Z", adds=10, dels=0, parents=""):
    return {
        "commit_sha": sha, "project_id": "", "project_name": project.split("/")[-1],
        "project_path": project, "author_name": "Dev", "author_email": email,
        "authored_date": date, "committed_date": date, "title": title,
        "additions": adds, "deletions": dels, "parent_ids": parents,
        "extracted_at": "2026-02-01T00:00:00Z",
    }


def diff(sha, path, text, adds=None, dels=None):
    lines = (text or "").split("\n") if text else []
    return {
        "commit_sha": sha, "file_path": path, "diff": text,
        "additions": adds if adds is not None else sum(1 for l in lines if l.startswith("+")),
        "deletions": dels if dels is not None else sum(1 for l in lines if l.startswith("-")),
        "extracted_at": "2026-02-01T00:00:00Z",
    }


def mr(iid, *, state="merged", source="feature/x", merge_sha=None, squash_sha=None,
       project_id="", name="repo", extracted_at="2026-02-01T00:00:00Z", web_url=""):
    return {
        "mr_id": f"id{iid}", "mr_iid": str(iid), "project_id": project_id,
        "project_name": name, "state": state, "source_branch": source,
        "target_branch": "main", "merge_commit_sha": merge_sha,
        "squash_commit_sha": squash_sha, "web_url": web_url, "extracted_at": extracted_at,
    }


def link(iid, sha, project_id="org/repo"):
    return {"mr_id": "", "mr_iid": str(iid), "commit_sha": sha, "project_id": project_id}


def run(storage, commits=(), diffs=(), mrs=(), links=()):
    storage.insert_batch("commits", list(commits))
    storage.insert_batch("diffs", list(diffs))
    storage.insert_batch("merge_requests", list(mrs))
    storage.insert_batch("mr_commits", list(links))
    return dw.compute(storage)


def added(*lines):
    """A GitHub-style patch body (hunk header, context, then the given lines)."""
    return "@@ -1,2 +1,4 @@\n context\n" + "\n".join(lines)


LONG = [f"+    result_{i} = compute_something_long_enough({i})" for i in range(6)]


# ── Rule 1: merge ────────────────────────────────────────────────────────────

def test_merge_with_custom_title_and_second_parent_in_table(duckdb_storage):
    res = run(duckdb_storage, [
        commit("base"), commit("feat"),
        commit("m1", title="release: staging → main (#734)", parents="base,feat"),
        commit("m2", title="Merge branch 'x'", parents="base,gone"),  # 2nd parent absent
        commit("plain", parents="base"),
    ])
    assert res.duplicates == {"m1": "merge"}


# ── Rule 2: squash ───────────────────────────────────────────────────────────

def test_squash_github_shape_covered_by_branch_commits(duckdb_storage):
    # project_id '' + short name on the MR, slug on mr_commits, empty mr_id.
    res = run(
        duckdb_storage,
        [commit("b1", adds=10), commit("b2", adds=10),
         commit("sq", adds=22, parents="base", title="feat (#7)"), commit("base")],
        mrs=[mr(7, merge_sha="sq")],
        # Duplicated link rows must not inflate coverage.
        links=[link(7, "b1"), link(7, "b1"), link(7, "b2")],
    )
    assert res.duplicates == {"sq": "squash"}  # 20 of 22 lines = 91%


def test_squash_gitlab_shape_and_below_threshold_is_kept(duckdb_storage):
    res = run(
        duckdb_storage,
        [commit("b1", adds=10), commit("sq", adds=20, parents="base"), commit("base"),
         commit("b3", adds=10), commit("sq2", adds=12, parents="base")],
        mrs=[mr(1, project_id="42", name="api", squash_sha="sq"),
             mr(2, project_id="42", name="api", squash_sha="sq2")],
        # GitLab can list the squash commit among the MR's commits: it must not
        # count towards its own coverage (that would be 20 of 20).
        links=[link(1, "b1", "42"), link(1, "sq", "42"), link(2, "b3", "42")],
    )
    # 10 of 20 lines = 50%: branch commits only partly present, keep the squash commit.
    # 10 of 12 lines = 83%: squash.
    assert res.duplicates == {"sq2": "squash"}


def test_same_named_gitlab_projects_do_not_share_merge_requests(duckdb_storage):
    # iids restart at 1 in every project; two projects both called "api".
    res = run(
        duckdb_storage,
        [commit("a1", adds=10), commit("asq", adds=10, parents="base"),
         commit("b1", adds=10), commit("bsq", adds=40, parents="base"), commit("base")],
        mrs=[mr(1, project_id="11", name="api", squash_sha="asq"),
             mr(1, project_id="22", name="api", squash_sha="bsq")],
        links=[link(1, "a1", "11"), link(1, "b1", "22")],
    )
    assert res.duplicates == {"asq": "squash"}  # 10 of 40 lines in the other project


def test_same_named_github_repos_do_not_share_pull_requests(duckdb_storage):
    res = run(
        duckdb_storage,
        [commit("a1", adds=10), commit("asq", adds=10, parents="base"),
         commit("b1", adds=10), commit("bsq", adds=40, parents="base"), commit("base")],
        mrs=[mr(1, name="web", merge_sha="asq", web_url="https://github.com/org-a/web/pull/1"),
             mr(1, name="web", merge_sha="bsq", web_url="https://github.com/Org-B/web/pull/1")],
        links=[link(1, "a1", "org-a/web"), link(1, "b1", "org-b/web")],  # slug case differs
    )
    assert res.duplicates == {"asq": "squash"}


def test_fast_forward_head_is_not_a_duplicate(duckdb_storage):
    # merge_commit_sha is the MR's own last commit: genuine work, stored once.
    res = run(
        duckdb_storage,
        [commit("c1", adds=10), commit("head", adds=10, parents="c1"),
         commit("p1", parents="base"), commit("pb", adds=1), commit("base")],
        mrs=[mr(1, source="staging", merge_sha="head"),
             mr(2, source="staging", merge_sha="p1")],
        # PR 2's own commit is stored, so p1 is a promotion of work kept elsewhere.
        links=[link(1, "c1"), link(1, "head"), link(2, "pb")],
    )
    assert res.duplicates == {"p1": "promotion"}  # a real promotion commit still counts


def test_squash_needs_exactly_one_parent_branch_commits_and_a_merged_mr(duckdb_storage):
    res = run(
        duckdb_storage,
        [commit("b1", adds=10),
         commit("two", adds=10, parents="base,b1"),   # merge: rule 1, not squash
         commit("none", adds=10, parents="base"),      # no branch commits in the table
         commit("open", adds=10, parents="base"),      # open PR: test-merge sha
         commit("zero", adds=0, parents="base"),       # zero lines: nothing to cover
         commit("base")],
        mrs=[mr(1, merge_sha="two"), mr(2, merge_sha="none"),
             mr(3, merge_sha="open", state="opened"), mr(4, merge_sha="zero")],
        links=[link(1, "b1"), link(2, "ghost"), link(3, "b1"), link(4, "b1")],
    )
    assert res.duplicates == {"two": "merge"}


def test_squash_coverage_boundary_is_inclusive(duckdb_storage):
    res = run(
        duckdb_storage,
        [commit("b1", adds=8), commit("sq_at", adds=10, parents="base"),
         commit("b2", adds=7), commit("sq_under", adds=10, parents="base"),
         commit("base")],
        mrs=[mr(1, squash_sha="sq_at"), mr(2, squash_sha="sq_under")],
        links=[link(1, "b1"), link(2, "b2")],
    )
    assert res.duplicates == {"sq_at": "squash"}  # 8/10 = exactly 80% is flagged; 7/10 kept


def test_merge_from_base_does_not_count_towards_squash_coverage(duckdb_storage):
    # The PR's real commits are not stored; its only stored own commit is a
    # merge of the base branch into it (2 parents, many lines). That must not
    # push coverage over 80% and drop the only copy of the work.
    res = run(
        duckdb_storage,
        [commit("sync", adds=20, parents="base,upstream"),
         commit("sq", adds=10, parents="base"), commit("base")],
        mrs=[mr(1, squash_sha="sq")],
        links=[link(1, "sync")],
    )
    assert res.duplicates == {}


def test_duplicate_mr_rows_use_the_newest_non_null_sha(duckdb_storage):
    # Re-extraction inserts the MR again: old NULL row + new filled row, and a
    # still newer row that lost the sha must not hide it.
    res = run(
        duckdb_storage,
        [commit("b1", adds=10), commit("sq", adds=10, parents="base"), commit("base")],
        mrs=[mr(7, extracted_at="2026-01-01T00:00:00Z"),
             mr(7, merge_sha="sq", extracted_at="2026-02-01T00:00:00Z"),
             mr(7, extracted_at="2026-03-01T00:00:00Z")],
        links=[link(7, "b1")],
    )
    assert res.duplicates == {"sq": "squash"}
    assert (res.merged_mrs, res.merged_mrs_with_sha) == (1, 1)


# ── Rule 3: promotion ────────────────────────────────────────────────────────

def test_promotion_from_long_lived_source_branch(duckdb_storage):
    # Each promotion lists one stored branch commit, too small to be a squash cover.
    res = run(
        duckdb_storage,
        [commit("p1", parents="base"), commit("p2", parents="base"),
         commit("p3", parents="base"), commit("f1", parents="base"), commit("base"),
         commit("b1", adds=1), commit("b2", adds=1), commit("b3", adds=1)],
        mrs=[mr(1, source="staging", merge_sha="p1"),
             mr(2, source="Release/1.2", merge_sha="p2"),
             mr(3, source="develop", squash_sha="p3", project_id="42", name="api"),
             mr(4, source="feature/staging-fix", merge_sha="f1")],
        links=[link(1, "b1"), link(2, "b2"), link(3, "b3", "42")],
    )
    assert res.duplicates == {"p1": "promotion", "p2": "promotion", "p3": "promotion"}


def test_promotion_counts_a_merge_from_base_as_stored_work(duckdb_storage):
    # The promotion guard asks "is any of the PR's work stored elsewhere?", merge
    # commits included; only the squash coverage sum ignores them.
    res = run(
        duckdb_storage,
        [commit("p", parents="base"), commit("mb", adds=1, parents="base,gone"),
         commit("base")],
        mrs=[mr(1, source="staging", merge_sha="p")],
        links=[link(1, "mb")],
    )
    assert res.duplicates == {"p": "promotion"}


def test_squash_from_a_fork_branch_named_main_is_the_only_copy(duckdb_storage):
    # A fork PR from `main` (GitHub stores only head.ref) or a GitLab fork MR from
    # `master`: none of its commits are in the table, so the squash commit is the
    # only stored copy. The branch name alone must not exclude it.
    res = run(
        duckdb_storage,
        [commit("sq", adds=10, parents="base"), commit("sq2", adds=10, parents="base"),
         commit("base")],
        mrs=[mr(5, source="main", squash_sha="sq"),
             mr(6, source="master", squash_sha="sq2", project_id="42", name="api")],
        links=[link(5, "ghost"), link(6, "ghost2", "42")],
    )
    assert res.duplicates == {}


def test_first_matching_rule_wins(duckdb_storage):
    res = run(
        duckdb_storage,
        [commit("base"), commit("feat"),
         commit("m", title="release: staging → main (#9)", parents="base,feat")],
        mrs=[mr(9, source="staging", merge_sha="m")],
    )
    assert res.duplicates == {"m": "merge"}  # also a promotion, merge comes first


def test_long_lived_branch_pattern():
    for name in ("main", "master", "develop", "dev", "development", "staging",
                 "production", "stage", "prod", "release/2.0", "support/1.x", "MAIN"):
        assert dw.is_long_lived(name), name
    for name in ("feature/main", "mainline", "hotfix/prod", "releases/2", "", None):
        assert not dw.is_long_lived(name), name


# ── Rule 4: copy ─────────────────────────────────────────────────────────────

def test_cherry_pick_is_a_copy_whatever_the_context_and_insertion_order(duckdb_storage):
    body = ["+    first = changed_line_number_one()", "-    old = gone()"]
    res = run(
        duckdb_storage,
        [commit("late", date="2026-01-20T10:00:00Z", email="DEV@Example.com"),
         commit("early", date="2026-01-10T10:00:00Z")],
        # Different hunk header and context: the signature ignores both.
        [diff("late", "a.py", "@@ -40,3 +41,3 @@\n other context\n" + "\n".join(body)),
         diff("early", "a.py", "@@ -1,3 +1,3 @@\n context\n" + "\n".join(body))],
    )
    assert res.duplicates == {"late": "copy"}


def test_changelog_lines_that_differ_are_not_copies(duckdb_storage):
    # Same title, same file, same +1/-0 every week: a looser signature flags these.
    res = run(
        duckdb_storage,
        [commit(f"c{i}", title="Added changelog", date=f"2026-01-{10 + i}T10:00:00Z")
         for i in range(4)],
        [diff(f"c{i}", "CHANGELOG.md", f"@@ -1 +1,2 @@\n+- entry number {i}") for i in range(4)],
    )
    assert res.duplicates == {}


def test_copy_needs_same_project_author_title_and_files(duckdb_storage):
    patch = added("+    same = content_of_the_change()")
    res = run(
        duckdb_storage,
        [commit("orig", date="2026-01-10T10:00:00Z"),
         commit("other_project", project="org/other", date="2026-01-11T10:00:00Z"),
         commit("other_author", email="x@example.com", date="2026-01-12T10:00:00Z"),
         commit("other_title", title="different", date="2026-01-13T10:00:00Z"),
         commit("other_file", date="2026-01-14T10:00:00Z")],
        [diff(s, "a.py", patch) for s in ("orig", "other_project", "other_author", "other_title")]
        + [diff("other_file", "b.py", patch)],
    )
    assert res.duplicates == {}


def test_copy_falls_back_to_line_counts_when_the_diff_is_missing(duckdb_storage):
    res = run(
        duckdb_storage,
        [commit("a", date="2026-01-10T10:00:00Z"), commit("b", date="2026-01-11T10:00:00Z"),
         commit("c", date="2026-01-12T10:00:00Z")],
        [diff("a", "logo.png", None, 0, 0), diff("b", "logo.png", "", 0, 0),  # NULL and ''
         diff("c", "logo.png", None, 3, 1)],
    )
    assert res.duplicates == {"b": "copy"}


def test_file_signature():
    base = "@@ -1 +1 @@\n ctx\n+a\n-b"
    assert dw.file_signature(base, 1, 1) == dw.file_signature("@@ -9 +9 @@\n x\n+a\n-b", 1, 1)
    assert dw.file_signature(base, 1, 1) != dw.file_signature("+a\n-c", 1, 1)
    # Header lines are excluded, a line that merely starts with '+' / '-' is kept.
    assert dw.file_signature("--- a/f\n+++ b/f\n+a\n-b", 1, 1) == dw.file_signature("+a\n-b", 1, 1)
    assert dw.file_signature(None, 3, 1) == "3:1"
    assert dw.file_signature("", 3, 1) == "3:1"


# ── Rule 5: moved code ───────────────────────────────────────────────────────

def moved_rows(res):
    return {(r["commit_sha"], r["file_path"]): r for r in res.moved}


def test_cross_repo_import_is_moved(duckdb_storage):
    res = run(
        duckdb_storage,
        [commit("poc", project="org/poc", date="2026-01-10T10:00:00Z", title="poc"),
         commit("imp", project="org/prod", date="2026-01-20T10:00:00Z", title="import")],
        [diff("poc", "src/server.py", added(*LONG)),
         diff("imp", "src/server.py", added(*LONG, "+    brand_new = a_line_written_only_here()"))],
    )
    row = moved_rows(res)[("imp", "src/server.py")]
    assert (row["qualifying_lines"], row["moved_lines"]) == (7, 6)
    assert row["moved_share"] == pytest.approx(6 / 7)
    assert row["rules_version"] == dw.DEDUPE_RULES_VERSION
    assert ("poc", "src/server.py") not in moved_rows(res)  # the original is not moved


def test_same_repo_move_to_another_path_is_moved(duckdb_storage):
    res = run(
        duckdb_storage,
        [commit("old", date="2026-01-10T10:00:00Z"), commit("mv", date="2026-01-20T10:00:00Z")],
        [diff("old", "lib/util.py", added(*LONG)), diff("mv", "core/util.py", added(*LONG))],
    )
    assert moved_rows(res)[("mv", "core/util.py")]["moved_share"] == 1.0


def test_rewriting_the_same_file_or_inside_one_commit_is_not_moved(duckdb_storage):
    res = run(
        duckdb_storage,
        [commit("a", date="2026-01-10T10:00:00Z"), commit("b", date="2026-01-20T10:00:00Z")],
        [diff("a", "x.py", added(*LONG)), diff("a", "y.py", added(*LONG[:3])),
         diff("b", "x.py", added(*LONG))],  # same path again: not a different file
    )
    # y.py repeats lines of x.py inside the first commit: same commit, so not moved.
    assert res.moved == []


def test_short_lines_and_context_do_not_count(duckdb_storage):
    short = "+    return 1"  # trimmed: 8 characters
    res = run(
        duckdb_storage,
        [commit("a", date="2026-01-10T10:00:00Z"), commit("b", date="2026-01-20T10:00:00Z")],
        [diff("a", "x.py", added(short, *LONG[:2])),
         diff("b", "y.py", added(short, *LONG[:2]) + "\n+++ b/y.py\n-" + LONG[2][1:])],
    )
    row = moved_rows(res)[("b", "y.py")]
    assert (row["qualifying_lines"], row["moved_lines"]) == (2, 2)


def test_boilerplate_lines_in_twenty_files_are_ignored(duckdb_storage):
    boiler = "+from some.framework.module import everything_commonly_needed"
    n = dw.MOVED_BOILERPLATE_FILES
    commits = [commit(f"c{i}", date=f"2026-01-{10 + i:02d}T10:00:00Z") for i in range(n + 1)]
    diffs = [diff(f"c{i}", f"f{i}.py", added(boiler)) for i in range(n)]
    # The last file also carries a genuinely moved line.
    diffs.append(diff(f"c{n}", "last.py", added(boiler, LONG[0])))
    diffs.append(diff("c0", "f0.py", added(LONG[0])))  # same commit/file as the first one
    res = run(duckdb_storage, commits, diffs)
    row = moved_rows(res)[(f"c{n}", "last.py")]
    # Only the non-boilerplate line is qualifying; it was first seen in f0.py.
    assert (row["qualifying_lines"], row["moved_lines"]) == (1, 1)
    # One fewer file and the same line is no longer boilerplate.
    assert not any(r["file_path"] == "f1.py" for r in res.moved)


def test_exactly_twenty_files_is_boilerplate(duckdb_storage):
    boiler = "+from some.framework.module import everything_commonly_needed"
    n = dw.MOVED_BOILERPLATE_FILES
    res = run(
        duckdb_storage,
        [commit(f"c{i}", date=f"2026-01-{10 + i:02d}T10:00:00Z") for i in range(n)],
        [diff(f"c{i}", f"f{i}.py", added(boiler)) for i in range(n)],
    )
    assert res.moved == []  # one file fewer (next test) leaves n - 2 moved rows


def test_fewer_than_twenty_files_is_not_boilerplate(duckdb_storage):
    boiler = "+from some.framework.module import everything_commonly_needed"
    n = dw.MOVED_BOILERPLATE_FILES - 1
    res = run(
        duckdb_storage,
        [commit(f"c{i}", date=f"2026-01-{10 + i:02d}T10:00:00Z") for i in range(n)],
        [diff(f"c{i}", f"f{i}.py", added(boiler)) for i in range(n)],
    )
    assert len(res.moved) == n - 1  # every file but the first


def test_min_chars_boundary_is_inclusive_after_trimming():
    n = dw.MOVED_MIN_CHARS
    at, under = "x" * n, "y" * (n - 1)
    # Padding is trimmed before counting: 25 characters inside, more outside.
    patch = added(f"+    {at}   ", f"+    {under}   ")
    assert dw.qualifying_fingerprints(patch) == {dw.fingerprint(at)}


def test_fingerprints_are_deterministic_64_bit():
    fp = dw.fingerprint("some line")
    assert fp == dw.fingerprint("some line") and 0 <= fp < 2 ** 64
    assert dw.qualifying_fingerprints(added(*LONG)) == {dw.fingerprint(l[1:].strip()) for l in LONG}


# ── Writing and reading the result ───────────────────────────────────────────

def test_write_rebuilds_tables_and_marks_the_run(duckdb_storage):
    assert dw.last_run(duckdb_storage) is None  # tables exist and are empty: never ran
    res = run(duckdb_storage, [commit("base"), commit("feat"),
                               commit("m", parents="base,feat")])
    dw.write(duckdb_storage, res)
    dw.write(duckdb_storage, res)  # a second run replaces, never appends
    rows = duckdb_storage.query("SELECT * FROM duplicate_commits")
    assert rows == [{"commit_sha": "m", "why": "merge",
                     "rules_version": dw.DEDUPE_RULES_VERSION}]
    marker = dw.last_run(duckdb_storage)
    assert marker["commits"] == 3 and marker["rules_version"] == dw.DEDUPE_RULES_VERSION
    assert duckdb_storage.count("dedupe_runs") == 1


def test_a_crash_mid_write_reads_as_never_ran(duckdb_storage, monkeypatch):
    res = run(duckdb_storage, [commit("base"), commit("feat"),
                               commit("m", parents="base,feat")])
    dw.write(duckdb_storage, res)
    assert dw.last_run(duckdb_storage) is not None

    real = duckdb_storage.replace_table

    def crash_on_moved(table, records):
        if table == "file_moved_share":
            raise RuntimeError("boom")
        real(table, records)

    monkeypatch.setattr(duckdb_storage, "replace_table", crash_on_moved)
    with pytest.raises(RuntimeError, match="boom"):
        dw.write(duckdb_storage, res)
    assert dw.last_run(duckdb_storage) is None  # the old marker must not survive


def test_clean_data_still_counts_as_a_run(duckdb_storage):
    dw.write(duckdb_storage, run(duckdb_storage, [commit("only")]))
    assert duckdb_storage.count("duplicate_commits") == 0
    assert dw.last_run(duckdb_storage) is not None


def test_dedupe_script_dry_run_and_real_run(tmp_dir, capsys):
    import dedupe
    from lib.storage import DuckDBStorage

    db = tmp_dir / "t.duckdb"
    cfg = tmp_dir / ".coding-productivity.env"
    cfg.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={db}\nPLATFORM=github\n"
                   "GITHUB_TOKEN=x\nREPOS=org/repo\n")
    cfg.chmod(0o600)
    st = DuckDBStorage(str(db))
    st.create_tables()
    st.insert_batch("commits", [commit("base"), commit("feat"),
                                commit("m", parents="base,feat")])
    st.insert_batch("merge_requests", [mr(1, merge_sha=None)])  # old install: no shas
    st.close()

    dedupe.main(["--config", str(cfg), "--dry-run"])
    out = capsys.readouterr().out
    assert "merge 1 / squash 0 / promotion 0 / copy 0" in out
    assert "carry no merge/squash commit sha" in out and "Dry run" in out
    st = DuckDBStorage(str(db))
    assert dw.last_run(st) is None
    st.close()

    dedupe.main(["--config", str(cfg)])
    st = DuckDBStorage(str(db))
    assert dw.last_run(st)["commits"] == 3 and st.count("duplicate_commits") == 1
    st.close()


def test_iter_query_streams_in_pages(duckdb_storage):
    duckdb_storage.insert_batch("commits", [commit(f"c{i}") for i in range(25)])
    rows = list(duckdb_storage.iter_query(
        "SELECT commit_sha FROM commits WHERE project_path = $p ORDER BY commit_sha",
        {"p": "org/repo"}, page_size=7))
    assert len(rows) == 25 and rows[0] == {"commit_sha": "c0"}


# ── Extractors store the shas ────────────────────────────────────────────────

def test_github_extractor_stores_merge_sha_for_merged_prs_only(duckdb_storage):
    import extract_github

    def pr(number, state, merged_at, sha):
        return {"id": number, "number": number, "state": state, "merged_at": merged_at,
                "closed_at": None, "title": "t", "user": {"login": "u"},
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
                "head": {"ref": "staging"}, "base": {"ref": "main"},
                "merge_commit_sha": sha, "html_url": ""}

    class Client:
        def get_pr_commits(self, repo, number):
            return []

        def get_pull_requests(self, repo, state="all"):
            yield pr(1, "closed", "2026-01-02T00:00:00Z", "abc")
            yield pr(2, "open", None, "testmerge")  # an open PR's sha is a test merge

    extract_github._extract_pull_requests(
        Client(), duckdb_storage, "org/repo", since=None, batch_size=100,
        anon_key=None, merges=None, mapping_path=None)
    rows = {r["mr_iid"]: r for r in duckdb_storage.query("SELECT * FROM merge_requests")}
    assert rows["1"]["merge_commit_sha"] == "abc" and rows["1"]["squash_commit_sha"] is None
    assert rows["1"]["source_branch"] == "staging" and rows["1"]["target_branch"] == "main"
    assert rows["2"]["merge_commit_sha"] is None


def test_gitlab_extractor_stores_merge_and_squash_sha(duckdb_storage):
    import extract_gitlab

    def mr_json(iid, state, merge, squash):
        return {"id": 100 + iid, "iid": iid, "title": "t", "state": state,
                "author": {"name": "Ann", "email": "ann@example.com"},
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
                "merged_at": None, "closed_at": None, "source_branch": "develop",
                "target_branch": "master", "merge_commit_sha": merge,
                "squash_commit_sha": squash, "web_url": ""}

    class Client:
        def get_merge_requests(self, project_id, state="all"):
            yield mr_json(1, "merged", None, "sq1")
            yield mr_json(2, "merged", "mc2", None)
            yield mr_json(3, "opened", "x", "y")

        def get_mr_commits(self, project_id, iid):
            return []

    extract_gitlab._extract_merge_requests(
        Client(), duckdb_storage, {"id": 42, "name": "api"}, since="2026-01-01",
        until="2026-12-31", batch_size=100, anon_key=None, merges=None, excluded=set())
    rows = {r["mr_iid"]: r for r in duckdb_storage.query("SELECT * FROM merge_requests")}
    assert (rows["1"]["merge_commit_sha"], rows["1"]["squash_commit_sha"]) == (None, "sq1")
    assert (rows["2"]["merge_commit_sha"], rows["2"]["squash_commit_sha"]) == ("mc2", None)
    assert (rows["3"]["merge_commit_sha"], rows["3"]["squash_commit_sha"]) == (None, None)
