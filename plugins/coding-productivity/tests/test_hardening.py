"""Regression tests for the review of the versioned-scoring work.

One section per finding (numbers match the review). Fake clients only: no
real Anthropic or BigQuery calls.
"""

import json
import sys
import types
from collections import namedtuple
from types import SimpleNamespace as NS

import pytest

import analyze
import rebase
import score_commits
from lib import duplicate_work as dw, scoring, scoring_meta, storage as storage_mod
from lib.config import Config
from .helpers import FakeClient, make_response
from .test_rebase import rebase_env  # noqa: F401  (fixture reused below)
from .test_score_run import config as score_config

FILES_A = [{"path": "src/auth.py", "score": 0.5, "category": "feature", "reasoning": "r"}]
FILES_B = [{"path": "src/login.py", "score": 0.4, "category": "bugfix", "reasoning": "r"}]
COMMIT = {"commit_sha": "abc123def456", "title": "feat: auth", "message": "feat: auth"}
DIFFS = [{"commit_sha": "abc123def456", "file_path": "src/auth.py",
          "additions": 50, "deletions": 10, "diff": "+x"}]
PV = scoring.ScoringSettings("claude-haiku-5-5", scoring.load_scoring_prompt()).prompt_version


@pytest.fixture
def rebase_env_factory(rebase_env):  # noqa: F811
    return lambda: rebase_env


@pytest.fixture
def score_env(tmp_dir, duckdb_storage, sample_commits, sample_diffs, monkeypatch):
    """score_commits against a real DuckDB with a fake Anthropic client."""
    duckdb_storage.insert_batch("commits", sample_commits)
    duckdb_storage.insert_batch("diffs", sample_diffs)
    monkeypatch.chdir(tmp_dir)
    monkeypatch.setattr(score_commits, "get_storage", lambda cfg: duckdb_storage)
    monkeypatch.setattr(duckdb_storage, "close", lambda: None)

    def run(client, model="claude-haiku-5-5", **kwargs):
        monkeypatch.setattr(score_commits, "_make_client", lambda key: client)
        return score_commits.score_commits(score_config(tmp_dir, model), workers=1, **kwargs)

    return NS(storage=duckdb_storage, run=run, tmp=tmp_dir)


def text_response(payload, model="claude-haiku-5-5-20260301", stop="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return NS(content=[NS(type="text", text=text)], stop_reason=stop, model=model,
              usage=NS(input_tokens=10, output_tokens=5))


# ── 1. BigQuery: $name placeholders are translated ───────────────────────────

Scalar = namedtuple("Scalar", "name type value")
Array = namedtuple("Array", "name type values")


class StrictBQClient:
    """Rejects ``$`` in SQL, like BigQuery does."""

    def __init__(self, project=None):
        self.queries = []
        self.rows = []

    def query(self, sql, job_config=None):
        if "$" in sql:
            raise ValueError("Syntax error: Illegal input character '$'")
        self.queries.append((sql, job_config))
        return NS(result=lambda: iter(self.rows))

    def close(self):
        pass


@pytest.fixture
def bq(monkeypatch):
    fake = types.SimpleNamespace(
        Client=StrictBQClient,
        QueryJobConfig=lambda default_dataset=None: NS(
            default_dataset=default_dataset, query_parameters=[]),
        ScalarQueryParameter=Scalar, ArrayQueryParameter=Array,
    )
    cloud = types.ModuleType("google.cloud")
    cloud.bigquery = fake
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake)
    return storage_mod.BigQueryStorage("proj", "ds")


def test_bigquery_translates_named_params_and_types_them(bq):
    bq.query("SELECT 1 FROM t WHERE a = $sha AND n > $n AND r IN ($repo_0, $repo_1) "
             "AND f = $flag AND x < $x",
             {"sha": "abc", "n": 3, "repo_0": "o/a", "repo_1": "o/b", "flag": True, "x": 1.5})
    sql, cfg = bq._client.queries[0]
    assert "$" not in sql and "@sha" in sql and "@repo_1" in sql
    params = {p.name: p for p in cfg.query_parameters}
    assert params["sha"] == Scalar("sha", "STRING", "abc")
    assert params["n"] == Scalar("n", "INT64", 3)
    assert params["x"] == Scalar("x", "FLOAT64", 1.5)
    assert params["flag"].type == "BOOL"
    assert cfg.default_dataset == "proj.ds"


def test_bigquery_list_params_become_array_parameters(bq):
    bq.query("SELECT 1 WHERE a IN UNNEST($shas)", {"shas": ["a", "b"]})
    _, cfg = bq._client.queries[0]
    assert cfg.query_parameters == [Array("shas", "STRING", ["a", "b"])]


def test_bigquery_leaves_unbound_dollar_alone_and_runs_without_params(bq):
    bq.query("SELECT COUNT(*) AS n FROM t")
    assert bq._client.queries[0][1].query_parameters == []


def test_every_parameterised_query_path_works_on_bigquery(bq):
    """Each call site that binds $params must survive a $-rejecting backend."""
    score_commits._fetch_diffs_for_commit(bq, "abc")
    rebase._prefetch(bq, ["abc", "def"])
    cfg = NS(REPOS=["org/repo"], EXCLUDED_DEVELOPERS=["bot@x"])
    analyze.check_single_scorer_model(bq, cfg, [("2026-01-01", "2026-02-01"),
                                                ("2026-03-01", "2026-04-01")])
    sql = bq._client.queries[-1][0]
    assert "@since_1" in sql and "@repo_0" in sql and "@excl_0" in sql


class ScriptedBQClient(StrictBQClient):
    """Answers the few lookups analyze makes before its main queries, with
    scores and a dedupe run present so every code path (the dedupe CTEs
    included) is built; every other query returns no rows."""

    def query(self, sql, job_config=None):
        super().query(sql, job_config)
        rows = []
        if "FROM dedupe_runs" in sql:
            rows = [{"rules_version": dw.DEDUPE_RULES_VERSION, "run_at": None,
                     "commits": 1, "merged_mrs": 0, "merged_mrs_with_sha": 0}]
        elif "AS cnt FROM" in sql:
            rows = [{"cnt": 5}]
        elif "COUNT(DISTINCT commit_sha) AS n FROM commits" in sql:
            rows = [{"n": 1}]
        elif "GROUP BY c.author_name, c.author_email" in sql and "WITH" not in sql:
            rows = [{"author_name": "a", "author_email": "a@x", "commits": 1}]
        return NS(result=lambda page_size=None: iter(rows))


# What DuckDB accepts and BigQuery rejects. The first run on a real BigQuery
# dataset died on date_trunc('month', <timestamp>); these cover its siblings.
DUCKDB_ONLY_SQL = ["date_trunc(", "FILTER (", "FILTER(", "EXTRACT(EPOCH", "::",
                   "ILIKE", "$"]


@pytest.mark.parametrize("scored_and_deduped", [True, False])
def test_every_analyze_query_is_bigquery_sql(bq, scored_and_deduped):
    # Unscored and never deduped, or scored with a dedupe run (the CTE path).
    bq._client = ScriptedBQClient() if scored_and_deduped else StrictBQClient()
    cfg = NS(REPOS=["org/repo"], EXCLUDED_DEVELOPERS=["bot@x"], DATE_FIELD="committed_date")
    since, until = "2026-01-01", "2026-02-01"
    people = analyze.load_people(bq, cfg)
    analyze.monthly_trends(bq, cfg, since, until, people=people)
    analyze.period_comparison(bq, cfg, since, until, "2026-02-01", "2026-03-01", people=people)
    analyze.author_productivity(bq, cfg, since, until, people=people)
    analyze.merge_velocity(bq, cfg, since, until, people=people)
    analyze.category_distribution(bq, cfg, since, until, people=people)
    analyze.code_efficiency(bq, cfg, since, until, people=people)
    analyze.dedupe_summary(bq, cfg, since, until)
    analyze.check_dedupe(bq)
    analyze.check_single_scorer_model(bq, cfg, [(since, until), ("2026-03-01", "2026-04-01")])

    queries = [q[0] for q in bq._client.queries]
    assert len(queries) > 20
    for sql in queries:
        for bad in DUCKDB_ONLY_SQL:
            assert bad not in sql, f"{bad!r} is not BigQuery SQL:\n{sql}"
    joined = "\n".join(queries)
    # Month bucketing goes through DATETIME_TRUNC, dates are compared as TIMESTAMPs,
    # and merge velocity measures time with TIMESTAMP_DIFF and counts with CASE.
    assert "DATETIME_TRUNC(DATETIME(COALESCE(c.committed_date, c.authored_date)), MONTH)" in joined
    assert "DATETIME_TRUNC(DATETIME(m.created_at), MONTH)" in joined
    assert "CAST(@since AS TIMESTAMP)" in joined and "CAST(@since_1 AS TIMESTAMP)" in joined
    assert "CAST(@b_start AS TIMESTAMP)" in joined
    assert "TIMESTAMP_DIFF(m.merged_at, m.created_at, MICROSECOND)" in joined
    if scored_and_deduped:
        assert "dd_files AS" in joined  # the dedupe-adjusted CTEs ran too


# ── 2. get_active: only "table missing" means legacy ─────────────────────────

class NotFound(Exception):  # same class name as google.api_core.exceptions.NotFound
    pass


class Boom:
    def __init__(self, exc):
        self.exc = exc

    def query(self, sql, params=None):
        raise self.exc

    def list_tables(self):
        return set()


def test_transient_error_is_not_mistaken_for_a_legacy_install():
    with pytest.raises(RuntimeError, match="503"):
        scoring_meta.get_active(Boom(RuntimeError("503 backend error")))
    with pytest.raises(RuntimeError):
        scoring_meta.next_free_version(Boom(RuntimeError("quota")))


def test_table_not_found_means_legacy():
    assert scoring_meta.get_active(Boom(NotFound("404 Not found: Table ds.scoring_meta"))).version == 1


def test_analyze_does_not_hide_transient_errors_as_no_scores():
    st = Boom(RuntimeError("network down"))
    with pytest.raises(RuntimeError):
        analyze._has_scores(st)


# ── 3. rebase sync fallback: progress survives an interrupt ──────────────────

class Interrupted(BaseException):
    """Stands in for the tool timeout / KeyboardInterrupt killing the run."""


def seed_failed(storage, n):
    shas = []
    for i in range(n):
        sha = f"sync{i:03d}"
        storage.insert_batch("commits", [{
            "commit_sha": sha, "title": "t", "message": "m", "authored_date": "2026-01-15T10:00:00Z",
            "author_email": "a", "author_name": "a", "project_path": "o/r"}])
        storage.insert_batch("diffs", [{"commit_sha": sha, "file_path": "src/auth.py",
                                        "additions": 10, "deletions": 0, "diff": "+x"}])
        shas.append(sha)
    storage.ensure_table("commit_scores_v2")
    storage.ensure_table("file_scores_v2")
    return {
        "commit_table": "commit_scores_v2", "file_table": "file_scores_v2",
        "failed": {s: "expired" for s in shas},
        "tokens": {"batch_input": 0, "batch_output": 0, "sync_input": 0, "sync_output": 0},
    }, shas


def test_sync_fallback_flushes_and_saves_progress_when_killed(duckdb_storage):
    state, shas = seed_failed(duckdb_storage, 5)
    saves = []
    settings = scoring.ScoringSettings("claude-haiku-5-5", "SYS")
    good = make_response(FILES_A)
    client = FakeClient([good, good, good, Interrupted()])

    with pytest.raises(Interrupted):
        rebase.sync_fallback(client, duckdb_storage, state, settings, 1,
                             lambda: saves.append(len(state["failed"])), flush_every=2)

    done = duckdb_storage.get_existing_shas("commit_scores_v2")
    assert len(done) == 3  # paid-for work was written, not lost with the buffer
    assert len(state["failed"]) == 2 and set(state["failed"]) == set(shas) - done
    assert saves and saves[-1] == 2  # state saved on the way out

    # The next run converges on what is left.
    client2 = FakeClient([good])
    rebase.sync_fallback(client2, duckdb_storage, state, settings, 1, lambda: None, flush_every=2)
    assert state["failed"] == {}
    assert duckdb_storage.count("commit_scores_v2") == 5


def test_sync_fallback_writes_incrementally_not_after_the_pool(duckdb_storage):
    state, _ = seed_failed(duckdb_storage, 6)
    writes = []
    real = duckdb_storage.insert_batch
    duckdb_storage.insert_batch = lambda t, r: (writes.append((t, len(r))), real(t, r))[1]
    rebase.sync_fallback(FakeClient([make_response(FILES_A)]), duckdb_storage, state,
                         scoring.ScoringSettings("claude-haiku-5-5", "SYS"), 2,
                         lambda: None, flush_every=2)
    commit_writes = [n for t, n in writes if t == "commit_scores_v2"]
    assert len(commit_writes) >= 3 and max(commit_writes) <= 2


# ── 4. Model output is validated ─────────────────────────────────────────────

BAD_PAYLOADS = {
    "files null": {"files": None},
    "files not a list": {"files": "a.py"},
    "files of strings": {"files": ["a.py"]},
    "files of null": {"files": [None]},
    "no path": {"files": [{"score": 0.5}]},
    "non-string path": {"files": [{"path": 3, "score": 0.5}]},
    "empty path": {"files": [{"path": "", "score": 0.5}]},
    "missing score": {"files": [{"path": "a.py"}]},
    "null score": {"files": [{"path": "a.py", "score": None}]},
    "string score": {"files": [{"path": "a.py", "score": "high"}]},
    "bool score": {"files": [{"path": "a.py", "score": True}]},
    "list score": {"files": [{"path": "a.py", "score": [0.5]}]},
    "not an object": ["files"],
}


@pytest.mark.parametrize("name", list(BAD_PAYLOADS))
def test_contract_violations_are_invalid_json(name):
    assert scoring.evaluate_response(text_response(BAD_PAYLOADS[name])) == (None, "invalid_json")


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_scores_are_rejected(literal):
    raw = '{"files": [{"path": "a.py", "score": %s}]}' % literal
    assert scoring.evaluate_response(text_response(raw)) == (None, "invalid_json")


def test_scores_are_clamped_to_unit_interval():
    payload = {"files": [{"path": "a.py", "score": 7}, {"path": "b.py", "score": -1},
                         {"path": "c.py", "score": "0.5"}]}
    result, reason = scoring.evaluate_response(text_response(payload))
    assert reason is None
    assert [f["score"] for f in result["files"]] == [1.0, 0.0, 0.5]
    rows = scoring.rows_from_result(
        "s", {"files": [{"path": "src/auth.py", "score": 7}]}, DIFFS, "m", "pv")
    assert rows["file_rows"][0]["score"] == 1.0
    assert rows["commit_row"]["total_productivity"] == 50.0  # 1.0 * 50 additions


def test_flags_are_flattened_to_strings():
    """A real Haiku 5.5 response returned a nested list; BigQuery rejects those."""
    payload = {"files": FILES_A, "flags": ["a", ["b", ["c"]], 3, None, {"x": 1}]}
    result, _ = scoring.evaluate_response(text_response(payload))
    assert result["flags"] == ["a", "b", "c"]
    for flags in (None, 7, {"x": 1}):
        result, _ = scoring.evaluate_response(text_response({"files": FILES_A, "flags": flags}))
        assert result["flags"] == []
    result, _ = scoring.evaluate_response(text_response({"files": FILES_A, "flags": "big"}))
    assert result["flags"] == ["big"]


def test_free_text_fields_are_coerced_not_fatal():
    payload = {"files": [{"path": "src/auth.py", "score": 0.5, "category": ["x"],
                          "reasoning": {"a": 1}}], "overall_category": ["feature"]}
    rows = scoring.rows_from_result("s", scoring.evaluate_response(text_response(payload))[0],
                                    DIFFS, "m", "pv")
    assert rows["file_rows"][0]["category"] == "chore"
    assert rows["file_rows"][0]["reasoning"] == ""
    assert rows["commit_row"]["overall_category"] == "unknown"


def test_calculate_productivity_never_raises_on_garbage():
    for bad in (None, {}, {"files": None}, {"files": ["a.py"]}, "text", []):
        assert scoring.calculate_productivity(bad, DIFFS) is None


def test_invalid_output_is_retried_then_accepted():
    client = FakeClient([text_response({"files": None}), make_response(FILES_A)])
    out = scoring.score_commit(client, scoring.ScoringSettings("claude-haiku-5-5", "S"),
                               COMMIT, DIFFS, sleep=lambda s: None)
    assert out.result is not None and out.failures == ["invalid_json"]


def test_malformed_batch_results_never_wedge_the_rebase(rebase_env_factory):
    env = rebase_env_factory()

    class Garbage(env.FakeBatches):
        def results(self, batch_id):
            good = make_response(FILES_B, model="claude-haiku-5-5-20260301")
            # r0: breaks the contract; r1 is fine; plus an item the code cannot
            # even classify and one with a foreign custom_id.
            yield NS(custom_id="r0", result=NS(type="succeeded", message=text_response(
                {"files": ["src/auth.py"]})))
            yield NS(custom_id="bogus", result=NS(type="succeeded", message=good))

            class Exploding:
                custom_id = "r1"

                @property
                def result(self):
                    raise RuntimeError("cannot read this item")
            yield Exploding()

    batches = Garbage()
    client = env.client(batches, make_response(FILES_A))
    assert env.run(client) == rebase.EXIT_DONE
    # Both commits ended up scored: the garbage ones through the sync retry.
    assert env.storage.count("commit_scores_v2") == 2
    state = json.loads((env.tmp / ".coding-productivity" / "rebase_state.json").read_text())
    assert state["batches"][0]["processed"] is True
    counts = state["batches"][0]["counts"]
    assert counts["invalid_json"] == 1 and counts["malformed_result"] == 1
    assert counts["unknown_custom_id"] == 1
    assert len(client.messages.calls) == 2  # exactly the two failed commits


# ── 5. File rows first, commit row last ──────────────────────────────────────

def spy(storage):
    order = []
    real = storage.insert_batch
    storage.insert_batch = lambda t, r: (order.append(t), real(t, r))[1]
    return order


def test_rebase_writes_file_rows_before_commit_rows(duckdb_storage):
    state, _ = seed_failed(duckdb_storage, 1)
    order = spy(duckdb_storage)
    rows = scoring.rows_from_result("sync000", {"files": FILES_A}, [
        {"file_path": "src/auth.py", "additions": 10, "deletions": 0}], "m", "pv")
    rebase._write_rows(duckdb_storage, state, [rows["commit_row"]], list(rows["file_rows"]))
    assert order == ["file_scores_v2", "commit_scores_v2"]


def test_crash_between_the_inserts_leaves_the_commit_unscored(duckdb_storage):
    state, _ = seed_failed(duckdb_storage, 1)
    rows = scoring.rows_from_result("sync000", {"files": FILES_A}, [
        {"file_path": "src/auth.py", "additions": 10, "deletions": 0}], "m", "pv")
    real = duckdb_storage.insert_batch

    def die_on_commit(table, records):
        if table == "commit_scores_v2":
            raise RuntimeError("connection lost")
        real(table, records)
    duckdb_storage.insert_batch = die_on_commit
    with pytest.raises(RuntimeError):
        rebase._write_rows(duckdb_storage, state, [rows["commit_row"]], list(rows["file_rows"]))
    assert duckdb_storage.get_existing_shas("commit_scores_v2") == set()  # resume retries it
    assert duckdb_storage.count("file_scores_v2") == 1


def test_score_commits_writes_file_rows_before_commit_rows(score_env):
    order = spy(score_env.storage)
    score_env.run(FakeClient([make_response(FILES_A)]))
    assert order[0] == "file_scores" and order[-1] == "commit_scores"
    assert order.index("file_scores") < order.index("commit_scores")


# ── 6. BigQuery streaming retry after create/update ──────────────────────────

class StreamClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def insert_rows_json(self, ref, rows):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


NO_FIELD = [{"index": 0, "errors": [{"reason": "invalid", "message": "no such field: scorer_model."}]}]


@pytest.fixture
def stream(bq, monkeypatch):
    sleeps = []
    monkeypatch.setattr(storage_mod, "_sleep", sleeps.append)
    bq.sleeps = sleeps
    return bq


def row():
    return [{"commit_sha": "a", "total_productivity": 1.0, "weighted_score": 0.5,
             "overall_category": "x", "scored_at": "2026-01-01T00:00:00Z",
             "scorer_model": "m", "prompt_version": "p"}]


def test_no_such_field_is_retried_with_backoff(stream):
    stream._client = StreamClient([NO_FIELD, NO_FIELD, []])
    stream.insert_batch("commit_scores", row())
    assert stream._client.calls == 3 and stream.sleeps == [2, 4]


def test_404_on_a_fresh_table_is_retried(stream):
    stream._client = StreamClient([NotFound("404 table"), []])
    stream.insert_batch("commit_scores", row())
    assert stream._client.calls == 2 and stream.sleeps == [2]


def test_other_insert_errors_fail_immediately(stream):
    stream._client = StreamClient([[{"index": 0, "errors": [{"message": "Nested arrays are not allowed"}]}]])
    with pytest.raises(RuntimeError, match="Nested arrays"):
        stream.insert_batch("commit_scores", row())
    assert stream._client.calls == 1 and stream.sleeps == []
    stream._client = StreamClient([RuntimeError("auth")])
    with pytest.raises(RuntimeError, match="auth"):
        stream.insert_batch("commit_scores", row())


def test_retry_gives_up_eventually(stream):
    stream._client = StreamClient([NO_FIELD] * 10)
    with pytest.raises(RuntimeError, match="no such field"):
        stream.insert_batch("commit_scores", row())
    assert stream._client.calls == len(storage_mod._STREAM_RETRY_DELAYS) + 1
    assert 240 <= sum(stream.sleeps) <= 300  # waits ~4-5 minutes in total


# ── 7. Legacy NULL series needs an explicit acknowledgement ──────────────────

LEGACY_ROW = {"commit_sha": "zzz", "total_productivity": 1.0, "weighted_score": 0.1,
              "overall_category": "chore", "scored_at": "2026-01-01T00:00:00Z"}


def test_legacy_null_series_requires_declared_model(score_env, capsys):
    score_env.storage.insert_batch("commit_scores", [LEGACY_ROW])
    with pytest.raises(SystemExit):
        score_env.run(FakeClient([make_response(FILES_A)]))
    assert "--legacy-model" in capsys.readouterr().err
    assert score_env.storage.count("commit_scores") == 1  # nothing appended


def test_declared_legacy_model_is_recorded_so_the_strict_check_applies(score_env):
    score_env.storage.insert_batch("commit_scores", [LEGACY_ROW])
    score_env.run(FakeClient([make_response(FILES_A)]), legacy_model="claude-haiku-5-5")
    active = scoring_meta.get_active(score_env.storage)
    assert (active.version, active.scorer_model, active.from_meta) == (1, "claude-haiku-5-5", True)
    assert score_env.storage.count("commit_scores") == 3
    # From now on the model check is strict, with no flag needed.
    score_env.run(FakeClient([make_response(FILES_A)]))
    with pytest.raises(SystemExit):
        score_env.run(FakeClient([make_response(FILES_A)]), model="claude-haiku-5-6")


def test_declared_legacy_model_must_match_the_configured_one(score_env, capsys):
    score_env.storage.insert_batch("commit_scores", [LEGACY_ROW])
    with pytest.raises(SystemExit):
        score_env.run(FakeClient([make_response(FILES_A)]), legacy_model="claude-haiku-4-5")
    err = capsys.readouterr().err
    assert "differs from SCORING_MODEL" in err and "rebase" in err
    assert scoring_meta.get_active(score_env.storage).from_meta is False


def test_empty_legacy_series_needs_no_acknowledgement(score_env):
    score_env.run(FakeClient([make_response(FILES_A)]))
    assert score_env.storage.count("commit_scores") == 2


# ── 8. prompt_version mismatch is an error, in score and in analyze ──────────

def test_score_refuses_a_changed_prompt_version(score_env, capsys):
    score_env.storage.ensure_table("commit_scores_v2")
    score_env.storage.ensure_table("file_scores_v2")
    scoring_meta.record(score_env.storage, 2, "active", "claude-haiku-5-5", "oldversion00")
    with pytest.raises(SystemExit):
        score_env.run(FakeClient([make_response(FILES_A)]))
    err = capsys.readouterr().err
    assert "oldversion00" in err and "--allow-prompt-change" in err
    assert score_env.storage.count("commit_scores_v2") == 0

    score_env.run(FakeClient([make_response(FILES_A)]), allow_prompt_change=True)
    assert score_env.storage.count("commit_scores_v2") == 2


def test_score_refuses_a_changed_prompt_version_in_a_legacy_series(score_env):
    score_env.storage.insert_batch("commit_scores", [{
        **LEGACY_ROW, "scorer_model": "claude-haiku-5-5-20260301", "prompt_version": "oldversion00"}])
    with pytest.raises(SystemExit):
        score_env.run(FakeClient([make_response(FILES_A)]))


def mk(sha, model, pv):
    return {"commit_sha": sha, "total_productivity": 10.0, "weighted_score": 0.5,
            "overall_category": "feature", "scored_at": "2026-03-27T00:00:00Z",
            "scorer_model": model, "prompt_version": pv}


CFG = NS(REPOS=[], EXCLUDED_DEVELOPERS=[])
RANGE = [("2026-01-01", "2026-04-01")]


def test_analyze_refuses_two_prompt_versions_of_the_same_model(duckdb_storage, sample_commits):
    duckdb_storage.insert_batch("commits", sample_commits)
    duckdb_storage.insert_batch("commit_scores", [
        mk("abc123def456", "claude-haiku-5-5", "aaaaaaaaaaaa"),
        mk("def456abc789", "claude-haiku-5-5", "bbbbbbbbbbbb")])
    with pytest.raises(analyze.MixedScorerModelError, match="prompt_version"):
        analyze.check_single_scorer_model(duckdb_storage, CFG, RANGE)
    with pytest.raises(analyze.MixedScorerModelError):  # never relaxed
        analyze.check_single_scorer_model(duckdb_storage, CFG, RANGE, allow_legacy_null=True)
    analyze.check_single_scorer_model(duckdb_storage, CFG, [("2026-01-01", "2026-02-01")])


def test_analyze_treats_a_missing_prompt_version_as_legacy(duckdb_storage, sample_commits):
    duckdb_storage.insert_batch("commits", sample_commits)
    duckdb_storage.insert_batch("commit_scores", [
        mk("abc123def456", "claude-haiku-5-5", None),
        mk("def456abc789", "claude-haiku-5-5", "aaaaaaaaaaaa")])
    with pytest.raises(analyze.MixedScorerModelError, match="--allow-legacy-null"):
        analyze.check_single_scorer_model(duckdb_storage, CFG, RANGE)
    analyze.check_single_scorer_model(duckdb_storage, CFG, RANGE, allow_legacy_null=True)


# ── 9. Post-processing is part of the instrument ─────────────────────────────

def test_post_processing_values_are_pinned():
    """If this fails you changed scoring post-processing. That changes every
    score: bump scoring.SCORING_LOGIC_VERSION (so prompt_version changes and
    existing series stop being extended), then update the pinned value."""
    assert scoring.logic_values_fingerprint() == "e5530b1404e4"


def test_post_processing_behaviour_is_pinned():
    """Golden outputs of the code in the post-processing path. A failure means
    behaviour changed: bump SCORING_LOGIC_VERSION and update these."""
    caps = [("README.md", 0.9, 10), ("a/b.json", 0.9, 600), ("a/b.json", 0.9, 100),
            ("package.json", 0.9, 600), ("src/x.py", 0.7, 1)]
    assert [scoring.apply_score_caps(*c) for c in caps] == [0.15, 0.05, 0.9, 0.9, 0.7]
    assert [scoring.auto_score_file(p) for p in ("yarn.lock", "src/x.py", "a/locales/en.yml")] == [0.0, None, 0.05]
    diffs = [{"file_path": "src/a.py", "additions": 10, "deletions": 5},
             {"file_path": "docs/r.md", "additions": 20, "deletions": 0},
             {"file_path": "x/data.json", "additions": 600, "deletions": 0}]
    res = {"files": [{"path": "src/a.py", "score": 0.8, "category": "feature", "reasoning": "r"},
                     {"path": "docs/r.md", "score": 0.9, "category": "docs"},
                     {"path": "x/data.json", "score": 1, "category": "weird"},
                     {"path": "not/in/diff.py", "score": 1, "category": "feature"}],
           "overall_category": "feature"}
    p = scoring.calculate_productivity(res, diffs)
    assert (p["total_productivity"], p["weighted_score"], p["total_lines"]) == (41.0, 0.0646, 635)
    assert [(f["file_path"], f["score"], f["category"]) for f in p["files"]] == [
        ("src/a.py", 0.8, "feature"), ("docs/r.md", 0.15, "docs"), ("x/data.json", 0.05, "chore")]


@pytest.mark.parametrize("mutate", [
    lambda mp: mp.setitem(scoring.AUTO_SCORE_PATTERNS, r".*\.foo$", 0.0),
    lambda mp: mp.setitem(scoring.MAX_SCORE_BY_EXTENSION, ".txt", 0.1),
    lambda mp: mp.setattr(scoring, "DIFF_SIZES", [5_000, 1_000]),
    lambda mp: mp.setattr(scoring, "MESSAGE_MAX_CHARS", 300),
    lambda mp: mp.setattr(scoring, "REASONING_MAX_CHARS", 500),
    lambda mp: mp.setattr(scoring, "LARGE_JSON_THRESHOLD", 100),
    lambda mp: mp.setattr(scoring, "LARGE_JSON_MAX_SCORE", 0.2),
    lambda mp: mp.setattr(scoring, "VALID_CATEGORIES", scoring.VALID_CATEGORIES | {"new"}),
    lambda mp: mp.setattr(scoring, "SCORING_LOGIC_VERSION", scoring.SCORING_LOGIC_VERSION + 1),
])
def test_every_post_processing_input_changes_prompt_version(mutate, monkeypatch):
    before = scoring.ScoringSettings("claude-haiku-5-5", "S").prompt_version
    mutate(monkeypatch)
    assert scoring.ScoringSettings("claude-haiku-5-5", "S").prompt_version != before


# ── 10. Missing prompt file is an error ──────────────────────────────────────

def test_missing_prompt_file_raises_instead_of_falling_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scoring, "_SCRIPT_DIR", tmp_path / "scripts")
    with pytest.raises(FileNotFoundError, match="will not run with a substitute"):
        scoring.load_scoring_prompt()
    with pytest.raises(SystemExit) as exit_info:
        scoring.settings_or_exit(NS(SCORING_MODEL="claude-haiku-5-5", SCORING_EFFORT="medium",
                                    SCORING_MAX_TOKENS=1000))
    assert exit_info.value.code == 1 and "Scoring prompt not found" in capsys.readouterr().err
    assert not hasattr(scoring, "FALLBACK_SYSTEM_PROMPT")


# ── 11. max_tokens is not part of the instrument ─────────────────────────────

def test_max_tokens_does_not_change_prompt_version():
    a = scoring.ScoringSettings("claude-haiku-5-5", "S", max_tokens=16000)
    b = scoring.ScoringSettings("claude-haiku-5-5", "S", max_tokens=32000)
    assert a.prompt_version == b.prompt_version
    assert b.build_params("x")["max_tokens"] == 32000  # still sent


def test_raising_max_tokens_mid_rebase_does_not_block_resume(rebase_env_factory):
    env = rebase_env_factory()

    class Pending(env.FakeBatches):
        ready = False

        def retrieve(self, batch_id):
            return NS(processing_status="ended" if self.ready else "in_progress",
                      request_counts=NS(processing=2, succeeded=0, errored=0))

    batches = Pending()
    client = env.client(batches, make_response(FILES_A))
    assert env.run(client) == rebase.EXIT_PENDING
    env.config.SCORING_MAX_TOKENS = 32000
    batches.ready = True
    assert env.run(client) == rebase.EXIT_DONE


# ── 12. Model allowlist ──────────────────────────────────────────────────────

@pytest.mark.parametrize("model", [
    "claude-haiku-5-5", "claude-haiku-5-5-20260301", "claude-sonnet-5", "claude-sonnet-5-5",
    "claude-sonnet-4-6", "claude-opus-4-6", "claude-opus-4-8", "claude-opus-5-5",
    "claude-fable-5-1", "claude-mythos-5-1"])
def test_known_models_get_adaptive_thinking_and_effort(model):
    p = scoring.ScoringSettings(model, "S", effort="high").build_params("x")
    assert p["thinking"] == {"type": "adaptive"} and p["output_config"] == {"effort": "high"}


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-haiku-4-5-20251001"])
def test_haiku4_still_sends_neither(model):
    p = scoring.ScoringSettings(model, "S").build_params("x")
    assert "thinking" not in p and "output_config" not in p


@pytest.mark.parametrize("model", [
    "claude-haiku-6", "claude-haiku-5x", "claude-sonnet-4-5", "claude-opus-4-5",
    "claude-sonnet-4", "gpt-4", "", "claude-future-1"])
def test_unknown_models_fail_fast(model):
    with pytest.raises(scoring.UnsupportedModelError, match="Unknown scoring model"):
        scoring.ScoringSettings(model, "S")


def test_unknown_model_is_a_config_error_and_a_clean_cli_exit(tmp_path, capsys):
    path = tmp_path / ".coding-productivity.env"
    path.write_text("PLATFORM=github\nGITHUB_TOKEN=x\nSTORAGE_BACKEND=duckdb\nDB_PATH=d\n"
                    "ANONYMIZATION_ENABLED=false\nSCORING_ENABLED=true\nANTHROPIC_API_KEY=k\n"
                    "SCORING_MODEL=claude-haiku-6\n")
    path.chmod(0o600)
    cfg = Config(path)
    assert any("Unknown scoring model" in e for e in cfg.validate())
    with pytest.raises(SystemExit) as exit_info:
        scoring.settings_or_exit(cfg)
    assert exit_info.value.code == 1 and "Unknown scoring model" in capsys.readouterr().err


# ── 13. The model that answered must be the model that was asked ─────────────

def test_response_from_another_model_is_a_failed_attempt():
    other = make_response(FILES_A, model="claude-haiku-4-5-20251001")
    out = scoring.score_commit(FakeClient([other]), scoring.ScoringSettings("claude-haiku-5-5", "S"),
                               COMMIT, DIFFS, sleep=lambda s: None)
    assert out.result is None and out.failures == ["model_mismatch"] * 3


def test_dated_snapshot_of_the_configured_model_is_accepted():
    ok = make_response(FILES_A, model="claude-haiku-5-5-20260301")
    result, reason = scoring.evaluate_response(ok, "claude-haiku-5-5")
    assert reason is None and result
    assert scoring.evaluate_response(make_response(FILES_A, model="claude-haiku-5-6"),
                                     "claude-haiku-5-5") == (None, "model_mismatch")


def test_batch_result_from_another_model_is_failed_and_retried():
    item = NS(custom_id="r0", result=NS(type="succeeded", message=make_response(
        FILES_A, model="claude-haiku-4-5-20251001")))
    assert rebase.classify_item(item, "claude-haiku-5-5")[3] == "model_mismatch"
    assert rebase.classify_item(item)[3] is None  # no expectation, no check


# ── 14. Batch API rejections go to the sync path ─────────────────────────────

def bad_request(message="invalid request", status=400):
    """An ``anthropic.APIStatusError`` without a real HTTP response (the SDK's
    HTTP client library differs between versions)."""
    anthropic = pytest.importorskip("anthropic")
    cls = anthropic.BadRequestError if status == 400 else anthropic.APIStatusError
    err = cls.__new__(cls)
    Exception.__init__(err, message)
    err.status_code = status
    return err


def test_rejected_request_is_isolated_and_scored_synchronously(rebase_env_factory):
    env = rebase_env_factory()
    err = bad_request()

    class Rejecting(env.FakeBatches):
        def create(self, requests):
            if any(r["custom_id"] == "r1" for r in requests):
                raise err
            return super().create(requests)

    batches = Rejecting()
    client = env.client(batches, make_response(FILES_B))
    assert env.run(client) == rebase.EXIT_DONE
    assert env.storage.count("commit_scores_v2") == 2
    assert len(batches.created) == 1 and [r["custom_id"] for r in batches.created[0]] == ["r0"]
    assert len(client.messages.calls) == 1  # only r1 went synchronous
    state = json.loads((env.tmp / ".coding-productivity" / "rebase_state.json").read_text())
    assert [b.get("rejected", False) for b in state["batches"]] == [False, True]
    assert (state["batches"][0]["end"], state["batches"][1]["start"]) == (1, 1)


def test_systematic_rejection_aborts_instead_of_bisecting_forever():
    err = bad_request()

    class AlwaysBad:
        calls = 0

        def create(self, requests):
            self.calls += 1
            raise err

    chunk = [(f"r{i}", {"model": "claude-haiku-5-5", "max_tokens": 10,
                        "messages": [{"role": "user", "content": "x"}]}) for i in range(64)]
    state = {"shas": [f"s{i}" for i in range(64)], "batches": [], "failed": {}}
    client = NS(messages=NS(batches=AlwaysBad()))
    with pytest.raises(RuntimeError, match="rejected 10 requests in a row.*billing or quota"):
        rebase._submit_chunk(client, state, chunk, lambda: None, {"rejected_in_a_row": 0})
    assert client.messages.batches.calls < 64 * 2  # stopped early


# ── N1. The prompt-change gate applies to a declared legacy series ───────────

def test_prompt_change_is_blocked_after_a_legacy_declaration(score_env, monkeypatch, capsys):
    score_env.storage.insert_batch("commit_scores", [LEGACY_ROW])
    score_env.run(FakeClient([make_response(FILES_A)]), legacy_model="claude-haiku-5-5")
    meta = score_env.storage.query("SELECT * FROM scoring_meta WHERE status = 'active'")
    assert [(r["version"], r["prompt_version"]) for r in meta] == [(1, PV)]  # P1 recorded
    assert score_env.storage.count("commit_scores") == 3

    # The scoring logic changes: P2.
    monkeypatch.setattr(scoring, "SCORING_LOGIC_VERSION", scoring.SCORING_LOGIC_VERSION + 1)
    score_env.storage.insert_batch("commits", [{
        "commit_sha": "new001", "title": "t", "message": "m", "authored_date": "2026-03-01T00:00:00Z",
        "author_email": "a", "author_name": "a", "project_path": "o/r"}])
    score_env.storage.insert_batch("diffs", [{"commit_sha": "new001", "file_path": "src/auth.py",
                                              "additions": 5, "deletions": 0, "diff": "+x"}])
    with pytest.raises(SystemExit):
        score_env.run(FakeClient([make_response(FILES_A)]))
    err = capsys.readouterr().err
    assert PV in err and "--allow-prompt-change" in err
    assert score_env.storage.count("commit_scores") == 3  # nothing appended under P2

    score_env.run(FakeClient([make_response(FILES_A)]), allow_prompt_change=True)
    assert score_env.storage.count("commit_scores") == 4


# ── N2. One bad file entry costs the file, not the commit ────────────────────

MIXED = {"files": [
    {"path": "src/auth.py", "score": 0.5, "category": "feature", "reasoning": "ok"},
    {"path": "src/null.py", "score": None},
    {"path": "src/missing.py"},
    "src/string.py",
    None,
    {"score": 0.4},
    {"path": "src/nan.py", "score": float("nan")},
]}


def test_malformed_entries_are_dropped_and_the_commit_is_kept():
    result, reason = scoring.evaluate_response(text_response(json.dumps(MIXED)))
    assert reason is None
    assert [f["path"] for f in result["files"]] == ["src/auth.py"]
    assert result["dropped_files"] == 6


def test_commit_is_rejected_when_no_valid_entry_remains():
    for files in ([], [None], ["a.py"], [{"path": "a.py", "score": None}], "a.py", None):
        assert scoring.evaluate_response(text_response({"files": files})) == (None, "invalid_json")


def test_dropped_entries_are_counted_and_the_valid_ones_stored():
    out = scoring.score_commit(FakeClient([text_response(MIXED)]),
                               scoring.ScoringSettings("claude-haiku-5-5", "S"),
                               COMMIT, DIFFS, sleep=lambda s: None)
    assert out.failures == [] and out.dropped_files == 6
    rows = scoring.rows_from_result("s", out.result, DIFFS, "m", "pv")
    assert [f["file_path"] for f in rows["file_rows"]] == ["src/auth.py"]
    log = scoring.FailureLog()
    log.record("2026-01", True, [], out.dropped_files)
    assert "dropped" in log.report() and "2026-01=6" in log.report()


def test_batch_result_with_a_bad_entry_is_kept(rebase_env_factory):
    env = rebase_env_factory()

    class OneBadEntry(env.FakeBatches):
        def results(self, batch_id):
            for req in self.created[0]:
                files = [{"path": "src/auth.py" if req["custom_id"] == "r0" else "src/login.py",
                          "score": 0.5, "category": "feature"}, {"path": "x.py", "score": None}]
                yield NS(custom_id=req["custom_id"], result=NS(type="succeeded", message=text_response(
                    {"files": files}, model=req["params"]["model"] + "-20260301")))

    client = env.client(OneBadEntry(), make_response(FILES_A))
    assert env.run(client) == rebase.EXIT_DONE
    assert len(client.messages.calls) == 0  # nothing needed the sync path
    state = json.loads((env.tmp / ".coding-productivity" / "rebase_state.json").read_text())
    assert state["batches"][0]["counts"] == {"succeeded": 2, "dropped_file_entries": 2}


# ── N3. Billing errors abort; 413 is handled like 400 ────────────────────────

def test_billing_error_aborts_without_bisecting_or_marking_commits(rebase_env_factory):
    env = rebase_env_factory()
    err = bad_request("Your credit balance is too low to access the Anthropic API.")

    class NoCredit(env.FakeBatches):
        calls = 0

        def create(self, requests):
            self.calls += 1
            raise err

    batches = NoCredit()
    with pytest.raises(RuntimeError, match="billing or credit"):
        env.run(env.client(batches, make_response(FILES_A)))
    assert batches.calls == 1  # no bisection
    state = json.loads((env.tmp / ".coding-productivity" / "rebase_state.json").read_text())
    assert state["failed"] == {} and state["batches"] == []  # resumable, nothing marked bad
    assert scoring_meta.get_active(env.storage).version == 1


@pytest.mark.parametrize("message", ["Your credit balance is too low", "billing issue",
                                     "You have reached your workspace usage limit"])
def test_billing_markers(message):
    assert rebase.is_billing_error(bad_request(message))
    assert not rebase.is_billing_error(bad_request("messages.0.content: Input should be a valid string"))


def test_413_is_isolated_like_400(rebase_env_factory):
    env = rebase_env_factory()
    err = bad_request("request_too_large", status=413)

    class TooLarge(env.FakeBatches):
        def create(self, requests):
            if any(r["custom_id"] == "r1" for r in requests):
                raise err
            return super().create(requests)

    client = env.client(TooLarge(), make_response(FILES_B))
    assert env.run(client) == rebase.EXIT_DONE
    assert env.storage.count("commit_scores_v2") == 2 and len(client.messages.calls) == 1


def test_other_status_errors_still_propagate(rebase_env_factory):
    env = rebase_env_factory()
    err = bad_request("overloaded", status=529)

    class Down(env.FakeBatches):
        def create(self, requests):
            raise err

    with pytest.raises(type(err)):
        env.run(env.client(Down(), make_response(FILES_A)))


# ── #9 gap. The diff truncation level actually used is recorded ──────────────

def test_commit_rows_record_the_diff_cap_of_the_successful_attempt():
    s = scoring.ScoringSettings("claude-haiku-5-5", "S")
    first, _ = scoring.score_and_build_rows(FakeClient([make_response(FILES_A)]), s, COMMIT, DIFFS)
    assert first["commit_row"]["diff_chars"] == 10_000

    retry, out = scoring.score_and_build_rows(
        FakeClient([text_response({"files": None}), make_response(FILES_A)]), s, COMMIT, DIFFS)
    assert retry["commit_row"]["diff_chars"] == 2_000 and out.failures == ["invalid_json"]
    third, _ = scoring.score_and_build_rows(
        FakeClient([text_response("nope"), text_response("nope"), make_response(FILES_A)]),
        s, COMMIT, DIFFS)
    assert third["commit_row"]["diff_chars"] == 1_000
    assert s.prompt_version  # unchanged by the retry level: it is recorded, not hashed


def test_sync_scoring_stores_the_diff_cap(score_env):
    score_env.run(FakeClient([make_response(FILES_A)]))
    assert {r["diff_chars"] for r in score_env.storage.query("SELECT diff_chars FROM commit_scores")} == {10_000}


def test_batch_rows_record_the_first_level(rebase_env_factory):
    env = rebase_env_factory()
    env.run(env.client(env.FakeBatches(), make_response(FILES_A)))
    rows = env.storage.query("SELECT diff_chars FROM commit_scores_v2")
    assert len(rows) == 2 and {r["diff_chars"] for r in rows} == {10_000}


# ── Duplicate paths: the first entry per file wins ───────────────────────────

TWO_FILES = [{"file_path": "src/a.py", "additions": 10, "deletions": 5},
             {"file_path": "src/b.py", "additions": 20, "deletions": 0}]


def test_duplicate_paths_keep_the_first_entry_and_are_counted():
    payload = {"files": [
        {"path": "src/a.py", "score": 0.8, "category": "feature"},
        {"path": "src/b.py", "score": 0.5, "category": "bugfix"},
        {"path": "src/a.py", "score": 0.1, "category": "chore"},   # repeat: dropped
        {"path": "src/a.py", "score": 0.9, "category": "docs"},    # repeat: dropped
    ]}
    result, reason = scoring.evaluate_response(text_response(payload))
    assert reason is None
    assert [(f["path"], f["score"]) for f in result["files"]] == [("src/a.py", 0.8), ("src/b.py", 0.5)]
    assert result["dropped_files"] == 2  # same counter as other dropped entries


def test_an_invalid_first_entry_does_not_shadow_a_valid_repeat():
    payload = {"files": [{"path": "src/a.py", "score": None},
                         {"path": "src/a.py", "score": 0.6}]}
    result, _ = scoring.evaluate_response(text_response(payload))
    assert [(f["path"], f["score"]) for f in result["files"]] == [("src/a.py", 0.6)]
    assert result["dropped_files"] == 1


def test_lines_and_file_rows_count_each_changed_file_once():
    once = {"files": [{"path": "src/a.py", "score": 0.8, "category": "feature"},
                      {"path": "src/b.py", "score": 0.5, "category": "bugfix"}]}
    twice = {"files": once["files"] + [{"path": "src/a.py", "score": 0.8, "category": "feature"}]}
    r1 = scoring.rows_from_result("s", once, TWO_FILES, "m", "pv")
    r2 = scoring.rows_from_result("s", twice, TWO_FILES, "m", "pv")
    assert r2["total_lines"] == r1["total_lines"] == 35  # sum of distinct changed files, not 50
    assert r2["commit_row"]["total_productivity"] == r1["commit_row"]["total_productivity"]
    assert r2["commit_row"]["weighted_score"] == r1["commit_row"]["weighted_score"]
    paths = [(f["commit_sha"], f["file_path"]) for f in r2["file_rows"]]
    assert len(paths) == len(set(paths)) == 2  # unique per (commit_sha, file_path)


def test_duplicates_are_deduplicated_when_calling_calculate_productivity_directly():
    raw = {"files": [{"path": "src/a.py", "score": 0.8}, {"path": "src/a.py", "score": 0.8}]}
    p = scoring.calculate_productivity(raw, TWO_FILES)
    assert p["total_lines"] == 15 and len(p["files"]) == 1
