"""score_commits and calibrate end to end on a fake client (no API calls)."""

import json
from types import SimpleNamespace as NS

import pytest

import calibrate
import score_commits
from lib import scoring, scoring_meta
from .helpers import FakeClient, make_response

FILES_A = [{"path": "src/auth.py", "score": 0.5, "category": "feature", "reasoning": "r"}]
FILES_B = [{"path": "src/login.py", "score": 0.4, "category": "bugfix", "reasoning": "r"}]


PV = scoring.ScoringSettings("claude-haiku-5-5", scoring.load_scoring_prompt()).prompt_version


def config(tmp_dir, model="claude-haiku-5-5"):
    return NS(
        _path=str(tmp_dir / ".coding-productivity.env"), EXCLUDED_DEVELOPERS=[],
        SCORING_MODEL=model, SCORING_EFFORT="medium", SCORING_MAX_TOKENS=16000,
        ANTHROPIC_API_KEY="k",
    )


@pytest.fixture
def env(tmp_dir, duckdb_storage, sample_commits, sample_diffs, monkeypatch):
    duckdb_storage.insert_batch("commits", sample_commits)
    duckdb_storage.insert_batch("diffs", sample_diffs)
    monkeypatch.chdir(tmp_dir)
    monkeypatch.setattr(score_commits, "get_storage", lambda cfg: duckdb_storage)
    monkeypatch.setattr(calibrate, "get_storage", lambda cfg: duckdb_storage)
    # score_commits closes its storage at the end; keep the fixture usable.
    monkeypatch.setattr(duckdb_storage, "close", lambda: None)
    return NS(storage=duckdb_storage, tmp=tmp_dir, monkeypatch=monkeypatch)


def test_score_writes_provenance_to_the_active_tables(env):
    env.monkeypatch.setattr(score_commits, "_make_client",
                            lambda key: FakeClient([make_response(FILES_A)]))
    results = score_commits.score_commits(config(env.tmp), workers=2, batch_size=10)
    assert len(results) == 2
    rows = env.storage.query("SELECT * FROM commit_scores")
    assert {r["scorer_model"] for r in rows} == {"claude-haiku-5-5-20260301"}
    assert all(len(r["prompt_version"]) == 12 for r in rows)
    files = env.storage.query("SELECT * FROM file_scores")
    assert files and all(f["scorer_model"] for f in files)


def test_score_continues_in_the_active_versioned_series(env):
    env.storage.ensure_table("commit_scores_v2")
    env.storage.ensure_table("file_scores_v2")
    scoring_meta.record(env.storage, 2, "active", "claude-haiku-5-5", "x")
    env.monkeypatch.setattr(score_commits, "_make_client",
                            lambda key: FakeClient([make_response(FILES_A)]))
    score_commits.score_commits(config(env.tmp), workers=1)
    assert env.storage.count("commit_scores_v2") == 2
    assert env.storage.count("commit_scores") == 0


def test_score_refuses_a_model_that_differs_from_the_active_series(env, capsys):
    scoring_meta.record(env.storage, 2, "active", "claude-haiku-4-5", "x")
    env.monkeypatch.setattr(score_commits, "_make_client", lambda key: FakeClient([make_response(FILES_A)]))
    with pytest.raises(SystemExit):
        score_commits.score_commits(config(env.tmp, "claude-haiku-5-5"), workers=1)
    assert "rebase" in capsys.readouterr().err


def test_score_refuses_legacy_table_written_by_another_model(env, capsys):
    env.storage.insert_batch("commit_scores", [{
        "commit_sha": "zzz", "total_productivity": 1.0, "weighted_score": 0.1,
        "overall_category": "chore", "scored_at": "2026-01-01T00:00:00Z",
        "scorer_model": "claude-haiku-4-5-20251001", "prompt_version": "p"}])
    env.monkeypatch.setattr(score_commits, "_make_client", lambda key: FakeClient([make_response(FILES_A)]))
    with pytest.raises(SystemExit):
        score_commits.score_commits(config(env.tmp, "claude-haiku-5-5"), workers=1)
    assert "claude-haiku-4-5-20251001" in capsys.readouterr().err


def test_failures_are_logged_per_month(env, capsys):
    refusal = make_response(FILES_A, stop_reason="refusal")
    env.monkeypatch.setattr(score_commits, "_make_client", lambda key: FakeClient([refusal]))
    env.monkeypatch.setattr(scoring.time, "sleep", lambda s: None)
    assert score_commits.score_commits(config(env.tmp), workers=1) == []
    out = capsys.readouterr().out
    assert "2026-01: 1 commit(s) unscored" in out and "refusal=3" in out
    assert env.storage.count("commit_scores") == 0


def test_calibrate_writes_json_and_never_touches_score_tables(env):
    env.storage.insert_batch("commit_scores", [
        {"commit_sha": "abc123def456", "total_productivity": 25.0, "weighted_score": 0.4167,
         "overall_category": "feature", "scored_at": "2026-01-16T00:00:00Z",
         "scorer_model": "claude-haiku-5-5-20260301", "prompt_version": PV},
        {"commit_sha": "def456abc789", "total_productivity": 2.0, "weighted_score": 0.25,
         "overall_category": "bugfix", "scored_at": "2026-02-16T00:00:00Z",
         "scorer_model": "claude-haiku-5-5-20260301", "prompt_version": PV}])
    client = FakeClient([make_response(FILES_A)])
    env.monkeypatch.setattr(calibrate, "_make_client", lambda key: client)
    out = env.tmp / "calib.json"

    payload = calibrate.calibrate(config(env.tmp), "claude-haiku-5-6", sample=10, workers=2,
                                  output=out, legacy_model=None, fresh_retest=False)

    assert out.is_file() and json.loads(out.read_text())["candidate_model"] == "claude-haiku-5-6"
    assert payload["verdict"]["verdict"] in ("COMPARABLE", "REBASE REQUIRED")
    assert len(client.messages.calls) == 4  # 2 commits x (current + candidate)
    models_called = {c["model"] for c in client.messages.calls}
    assert models_called == {"claude-haiku-5-5", "claude-haiku-5-6"}
    assert env.storage.count("commit_scores") == 2  # unchanged
    assert env.storage.count("file_scores") == 0
    assert env.storage.count("scoring_meta") == 0


def test_calibrate_refuses_stored_scores_from_another_prompt_version(env, capsys):
    env.storage.insert_batch("commit_scores", [
        {"commit_sha": "abc123def456", "total_productivity": 25.0, "weighted_score": 0.4,
         "overall_category": "feature", "scored_at": "2026-01-16T00:00:00Z",
         "scorer_model": "claude-haiku-5-5-20260301", "prompt_version": "oldprompt000"}])
    env.monkeypatch.setattr(calibrate, "_make_client", lambda key: FakeClient([make_response(FILES_A)]))
    with pytest.raises(SystemExit):
        calibrate.calibrate(config(env.tmp), "claude-haiku-5-6", 10, 1, env.tmp / "o.json", None, False)
    err = capsys.readouterr().err
    assert "oldprompt000" in err and "--fresh-retest" in err
    # A fresh second pass does not depend on the stored scores' provenance.
    calibrate.calibrate(config(env.tmp), "claude-haiku-5-6", 10, 1, env.tmp / "o2.json", None, True)


def test_calibrate_needs_provenance_for_the_noise_floor(env, capsys):
    env.storage.insert_batch("commit_scores", [
        {"commit_sha": "abc123def456", "total_productivity": 25.0, "weighted_score": 0.4,
         "overall_category": "feature", "scored_at": "2026-01-16T00:00:00Z"}])  # legacy NULL model
    env.monkeypatch.setattr(calibrate, "_make_client", lambda key: FakeClient([make_response(FILES_A)]))
    with pytest.raises(SystemExit):
        calibrate.calibrate(config(env.tmp), "claude-haiku-5-6", 10, 1, env.tmp / "o.json", None, False)
    assert "--legacy-model" in capsys.readouterr().err

    # ... or declared explicitly, or avoided with a fresh second pass.
    calibrate.calibrate(config(env.tmp), "claude-haiku-5-6", 10, 1, env.tmp / "o.json",
                        "claude-haiku-5-5", False)
    calibrate.calibrate(config(env.tmp), "claude-haiku-5-6", 10, 1, env.tmp / "o2.json", None, True)
