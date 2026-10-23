"""analyze.py with duplicate-work detection: exclusions, discount, warnings;
plus the schema migration of merge_requests and the BigQuery replace path."""

import sys
import types
from types import SimpleNamespace as NS

import pytest

import analyze
from lib import duplicate_work as dw
from lib import schema
from lib.storage import DuckDBStorage

from .test_duplicate_work import commit, diff, mr, run
from .test_migration import Field, _FakeBQClient, _FakeTable, _columns

CFG = NS(REPOS=[], EXCLUDED_DEVELOPERS=[])
JAN = ("2026-01-01", "2026-02-01")


def score(sha, total, weighted):
    return {"commit_sha": sha, "total_productivity": total, "weighted_score": weighted,
            "overall_category": "feature", "scored_at": "2026-03-01T00:00:00Z",
            "scorer_model": "m", "prompt_version": "p"}


def fscore(sha, path, s, at="2026-03-01T00:00:00Z"):
    return {"commit_sha": sha, "file_path": path, "score": s, "category": "feature",
            "reasoning": "", "scored_at": at, "scorer_model": "m", "prompt_version": "p"}


def marker(storage, commits=4, version=dw.DEDUPE_RULES_VERSION):
    storage.insert_batch("dedupe_runs", [{
        "rules_version": version, "run_at": "2026-03-02T00:00:00Z", "commits": commits,
        "merged_mrs": 0, "merged_mrs_with_sha": 0}])


@pytest.fixture
def world(duckdb_storage):
    """k1 normal, k2 flagged copy, k3 with a mostly-moved file, k4 without file rows."""
    st = duckdb_storage
    st.insert_batch("commits", [
        commit("k1", date="2026-01-10T10:00:00Z", adds=20),
        commit("k2", date="2026-01-11T10:00:00Z", adds=20),
        commit("k3", date="2026-01-12T10:00:00Z", adds=15),
        commit("k4", date="2026-01-13T10:00:00Z", adds=4, dels=2),
    ])
    st.insert_batch("diffs", [
        diff("k1", "a.py", "+x", 20, 0), diff("k2", "a.py", "+x", 20, 0),
        diff("k3", "b.py", "+x", 10, 0), diff("k3", "c.py", "+x", 5, 0),
    ])
    st.insert_batch("commit_scores", [
        score("k1", 999.0, 0.9),   # stored total is stale: files say 10
        score("k2", 50.0, 0.5), score("k3", 80.0, 0.5), score("k4", 7.0, 0.3),
    ])
    st.insert_batch("file_scores", [
        fscore("k1", "a.py", 0.5), fscore("k1", "a.py", 0.5),  # repeated row: count once
        fscore("k2", "a.py", 0.5), fscore("k3", "b.py", 1.0), fscore("k3", "c.py", 0.4),
    ])
    st.insert_batch("duplicate_commits", [
        {"commit_sha": "k2", "why": "copy", "rules_version": dw.DEDUPE_RULES_VERSION}])
    st.insert_batch("file_moved_share", [
        {"commit_sha": "k3", "file_path": "b.py", "qualifying_lines": 4, "moved_lines": 3,
         "moved_share": 0.75, "rules_version": dw.DEDUPE_RULES_VERSION},
        {"commit_sha": "k3", "file_path": "c.py", "qualifying_lines": 5, "moved_lines": 2,
         "moved_share": 0.4, "rules_version": dw.DEDUPE_RULES_VERSION},  # below 0.5: kept
    ])
    marker(st)
    return st


def test_monthly_trends_exclude_duplicates_and_apply_the_discount(world):
    row = analyze.monthly_trends(world, CFG, *JAN)[0]
    assert row["commits"] == 3  # k2 excluded
    # k1: 0.5*20 = 10 (recomputed from deduped file rows, not the stored 999)
    # k3: 1.0*10*0.25 + 0.4*5*1 = 4.5 ; k4: no file rows, stored 7
    assert row["total_productivity"] == pytest.approx(21.5)
    # weighted: k1 10/20, k3 4.5/(10*0.25 + 5), k4 stored 0.3
    assert row["avg_weighted_score"] == pytest.approx((0.5 + 0.6 + 0.3) / 3, abs=1e-3)


def test_without_a_dedupe_run_nothing_changes(world):
    world._conn.execute("DELETE FROM dedupe_runs")
    row = analyze.monthly_trends(world, CFG, *JAN)[0]
    assert row["commits"] == 4
    assert row["total_productivity"] == pytest.approx(999 + 50 + 80 + 7)


def test_other_queries_use_the_same_source(world):
    authors = analyze.author_productivity(world, CFG, *JAN)
    assert authors[0]["commits"] == 3 and authors[0]["total_productivity"] == pytest.approx(21.5)
    cats = analyze.category_distribution(world, CFG, *JAN)
    assert cats[0]["commit_count"] == 3
    cmp_rows = analyze.period_comparison(world, CFG, *JAN, "2026-02-01", "2026-03-01")
    assert cmp_rows[0]["commits"] == 3 and cmp_rows[0]["total_productivity"] == pytest.approx(21.5)
    eff = analyze.code_efficiency(world, CFG, *JAN)
    assert eff[0]["total_additions"] == 20 + 15 + 4  # k2 excluded


def test_unscored_lines_are_excluded_and_discounted(duckdb_storage):
    st = duckdb_storage
    st.insert_batch("commits", [commit("k1", adds=20), commit("k2", adds=20),
                                commit("k3", adds=15), commit("k4", adds=4, dels=2)])
    st.insert_batch("diffs", [diff("k1", "a.py", "+x", 20, 0), diff("k3", "b.py", "+x", 10, 0),
                              diff("k3", "c.py", "+x", 5, 0)])
    st.insert_batch("duplicate_commits", [
        {"commit_sha": "k2", "why": "copy", "rules_version": 1}])
    st.insert_batch("file_moved_share", [
        {"commit_sha": "k3", "file_path": "b.py", "qualifying_lines": 4, "moved_lines": 3,
         "moved_share": 0.75, "rules_version": 1}])
    marker(st)
    row = analyze.monthly_trends(st, CFG, *JAN)[0]
    assert row["commits"] == 3
    assert row["total_lines_changed"] == pytest.approx(20 + (2.5 + 5) + 6)  # k4 has no diffs


def test_summary_line(world):
    line = analyze.dedupe_summary(world, CFG, *JAN)
    # kept productivity before the discount: k1 10 + k3 (10 + 2) = 22; after: 14.5
    assert line == ("excluded: 1 commits (merge 0 / squash 0 / promotion 0 / copy 1), "
                    "moved-code discount 34.1% of productivity")


def test_summary_is_none_when_never_run(world):
    world._conn.execute("DELETE FROM dedupe_runs")
    assert analyze.dedupe_summary(world, CFG, *JAN) is None


def test_end_to_end_dedupe_then_analyze(duckdb_storage):
    st = duckdb_storage
    patch = "@@ -1 +1 @@\n+    something_long_enough_to_count_here()"
    run(st, [commit("orig", date="2026-01-10T10:00:00Z", adds=1),
             commit("pick", date="2026-01-20T10:00:00Z", adds=1)],
        [diff("orig", "a.py", patch), diff("pick", "a.py", patch)])
    st.insert_batch("commit_scores", [score("orig", 1.0, 1.0), score("pick", 1.0, 1.0)])
    st.insert_batch("file_scores", [fscore("orig", "a.py", 1.0), fscore("pick", "a.py", 1.0)])
    dw.write(st, dw.compute(st))
    row = analyze.monthly_trends(st, CFG, *JAN)[0]
    assert row["commits"] == 1 and row["total_productivity"] == pytest.approx(1.0)


# ── Warnings and --require-dedupe ────────────────────────────────────────────

def test_never_run_warns_and_require_refuses(duckdb_storage):
    warnings = analyze.check_dedupe(duckdb_storage)
    assert len(warnings) == 1 and "never run" in warnings[0]
    with pytest.raises(analyze.DedupeRequiredError):
        analyze.check_dedupe(duckdb_storage, require=True)


def test_stale_run_warns(world):
    assert analyze.check_dedupe(world) == []
    world.insert_batch("commits", [commit("new")])
    assert "Run dedupe again" in analyze.check_dedupe(world)[0]
    world._conn.execute("UPDATE dedupe_runs SET rules_version = 0, commits = 5")
    assert "rules v0" in analyze.check_dedupe(world)[0]


def _config(tmp_dir):
    db = tmp_dir / "t.duckdb"
    cfg = tmp_dir / ".coding-productivity.env"
    cfg.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={db}\n")
    cfg.chmod(0o600)
    return cfg, db


def test_cli_warns_on_stderr_and_proceeds_without_dedupe(tmp_dir, capsys, monkeypatch):
    cfg, db = _config(tmp_dir)
    st = DuckDBStorage(str(db))
    st.create_tables()
    st.insert_batch("commits", [commit("a", date="2026-01-10T10:00:00Z")])
    st.close()
    monkeypatch.setattr(sys, "argv", ["analyze", "--config", str(cfg), "--query",
                                      "monthly_trends", "--since", JAN[0], "--until", JAN[1],
                                      "--format", "json"])
    analyze.main()
    out = capsys.readouterr()
    assert "never run" in out.err
    assert out.out.lstrip().startswith("[")  # JSON stays clean on stdout


def test_cli_require_dedupe_exits_2(tmp_dir, capsys, monkeypatch):
    cfg, db = _config(tmp_dir)
    st = DuckDBStorage(str(db))
    st.create_tables()
    st.close()
    monkeypatch.setattr(sys, "argv", ["analyze", "--config", str(cfg), "--query",
                                      "monthly_trends", "--since", JAN[0], "--until", JAN[1],
                                      "--require-dedupe"])
    with pytest.raises(SystemExit) as exc:
        analyze.main()
    assert exc.value.code == 2
    assert "never run" in capsys.readouterr().err


def test_cli_prints_the_excluded_line(world, tmp_dir, capsys, monkeypatch):
    cfg, db = _config(tmp_dir)
    world.close()
    # Reuse the populated database file through the config.
    cfg.write_text(f"STORAGE_BACKEND=duckdb\nDB_PATH={world._db_path}\n")
    cfg.chmod(0o600)
    monkeypatch.setattr(sys, "argv", ["analyze", "--config", str(cfg), "--query",
                                      "monthly_trends", "--since", JAN[0], "--until", JAN[1]])
    analyze.main()
    out = capsys.readouterr().out
    assert "excluded: 1 commits (merge 0 / squash 0 / promotion 0 / copy 1), " \
           "moved-code discount 34.1% of productivity" in out


# ── merge_requests migration ─────────────────────────────────────────────────

OLD_MR = ("CREATE TABLE merge_requests (mr_id VARCHAR, mr_iid VARCHAR, project_id VARCHAR, "
          "project_name VARCHAR, title VARCHAR, state VARCHAR, merged_at TIMESTAMP)")
NEW_MR_COLUMNS = {"merge_commit_sha", "squash_commit_sha", "source_branch", "target_branch"}


def test_duckdb_migrates_merge_requests(tmp_dir):
    st = DuckDBStorage(str(tmp_dir / "old.duckdb"))
    st._conn.execute(OLD_MR)
    st._conn.execute("INSERT INTO merge_requests VALUES ('1', '7', '', 'repo', 't', 'merged', NULL)")
    st.create_tables()
    st.create_tables()  # idempotent
    assert NEW_MR_COLUMNS <= _columns(st, "merge_requests")
    old = st.query("SELECT * FROM merge_requests")[0]
    assert old["mr_iid"] == "7" and old["merge_commit_sha"] is None
    # New tables exist and take rows.
    for table in ("duplicate_commits", "file_moved_share", "dedupe_runs"):
        assert table in st.list_tables()
    st.close()


def test_schema_has_the_new_tables():
    assert NEW_MR_COLUMNS <= set(schema.get_columns("merge_requests"))
    assert schema.get_columns("duplicate_commits") == ["commit_sha", "why", "rules_version"]
    assert schema.get_columns("file_moved_share") == [
        "commit_sha", "file_path", "qualifying_lines", "moved_lines", "moved_share",
        "rules_version"]


class _Job:
    def result(self):
        return None


class _ReplaceClient(_FakeBQClient):
    def __init__(self, project=None):
        super().__init__(project)
        self.queries, self.loads = [], []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        return _Job()

    def load_table_from_json(self, rows, ref, job_config=None):
        self.loads.append((ref, rows, job_config))
        return _Job()


def _fake_bq(monkeypatch):
    fake = types.SimpleNamespace(
        Client=_ReplaceClient, Table=_FakeTable, SchemaField=Field,
        LoadJobConfig=lambda **kw: types.SimpleNamespace(**kw))
    cloud = types.ModuleType("google.cloud")
    cloud.bigquery = fake
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake)


def test_bigquery_migrates_merge_requests_and_creates_new_tables(monkeypatch):
    _fake_bq(monkeypatch)
    from lib.storage import BigQueryStorage

    st = BigQueryStorage("proj", "ds")
    old = "proj.ds.merge_requests"
    st._client.tables[old] = _FakeTable(old, [Field("mr_id", "STRING"), Field("state", "STRING")])
    st.create_tables()
    names = {f.name for f in st._client.tables[old].schema}
    assert NEW_MR_COLUMNS <= names
    assert all(f.mode == "NULLABLE" for f in st._client.tables[old].schema)
    assert {"proj.ds.duplicate_commits", "proj.ds.file_moved_share",
            "proj.ds.dedupe_runs"} <= set(st._client.tables)


def test_bigquery_replace_table_uses_a_truncating_load_job(monkeypatch):
    _fake_bq(monkeypatch)
    from lib.storage import BigQueryStorage

    st = BigQueryStorage("proj", "ds")
    st.replace_table("duplicate_commits", [{"commit_sha": "a", "why": "merge", "extra": 1}])
    ref, rows, cfg = st._client.loads[0]
    assert ref == "proj.ds.duplicate_commits"
    assert rows == [{"commit_sha": "a", "why": "merge", "rules_version": None}]
    assert cfg.write_disposition == "WRITE_TRUNCATE"
    st.replace_table("duplicate_commits", [])  # an empty load job is rejected: TRUNCATE
    assert st._client.queries == ["TRUNCATE TABLE `proj.ds.duplicate_commits`"]


class _Row:
    """Stands in for a BigQuery ``Row``: mapping-like, but not a dict."""

    def __init__(self, **values):
        self._values = values

    def keys(self):
        return self._values.keys()

    def __getitem__(self, key):
        return self._values[key]


class _PagedJob:
    def __init__(self, client):
        self._client = client

    def result(self, page_size=None):
        self._client.page_sizes.append(page_size)
        return iter([_Row(a=1, b="x"), _Row(a=2, b="y")])


class _QueryClient(_ReplaceClient):
    def __init__(self, project=None):
        super().__init__(project)
        self.job_configs, self.page_sizes = [], []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        self.job_configs.append(job_config)
        return _PagedJob(self)


def test_bigquery_iter_query_rewrites_params_pages_and_yields_dicts(monkeypatch):
    _fake_bq(monkeypatch)
    bq = sys.modules["google.cloud.bigquery"]
    bq.Client = _QueryClient
    bq.QueryJobConfig = lambda **kw: types.SimpleNamespace(**kw)
    bq.ScalarQueryParameter = lambda name, kind, value: (name, kind, value)
    from lib.storage import BigQueryStorage

    st = BigQueryStorage("proj", "ds")
    rows = st.iter_query("SELECT a, b FROM t WHERE p = $p", {"p": "x"}, page_size=7)
    assert not isinstance(rows, list)  # a generator: nothing is sent until iterated
    got = list(rows)
    assert st._client.queries == ["SELECT a, b FROM t WHERE p = @p"]
    assert st._client.page_sizes == [7]
    assert st._client.job_configs[0].query_parameters == [("p", "STRING", "x")]
    assert got == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert all(type(r) is dict for r in got)


# ── Moved-share boundary and the commit-count staleness check ────────────────

@pytest.mark.parametrize("share, productivity", [
    (0.5, 5.0),    # exactly at the threshold: discounted, factor 1 - 0.5
    (0.49, 10.0),  # just below: kept whole
])
def test_moved_share_threshold_is_inclusive(duckdb_storage, share, productivity):
    st = duckdb_storage
    st.insert_batch("commits", [commit("m1", date="2026-01-10T10:00:00Z", adds=10)])
    st.insert_batch("diffs", [diff("m1", "a.py", "+x", 10, 0)])
    st.insert_batch("commit_scores", [score("m1", 99.0, 0.5)])
    st.insert_batch("file_scores", [fscore("m1", "a.py", 1.0)])
    st.insert_batch("file_moved_share", [
        {"commit_sha": "m1", "file_path": "a.py", "qualifying_lines": 10,
         "moved_lines": 5, "moved_share": share, "rules_version": dw.DEDUPE_RULES_VERSION}])
    marker(st, commits=1)
    row = analyze.monthly_trends(st, CFG, *JAN)[0]
    assert row["total_productivity"] == pytest.approx(productivity)


def test_a_repeated_sha_does_not_make_the_run_look_stale(duckdb_storage):
    st = duckdb_storage
    # Dedupe records distinct shas: 2 here, though the table has 3 rows.
    st.insert_batch("commits", [commit("d1"), commit("d1"), commit("d2")])
    marker(st, commits=2)
    assert analyze.check_dedupe(st) == []
