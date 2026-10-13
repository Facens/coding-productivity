"""Request shape, response handling, provenance rows (no API calls)."""

import json
from types import SimpleNamespace as NS

import pytest

from .helpers import FakeClient, make_response
from lib import scoring
from lib.config import Config

FILES = [{"path": "src/auth.py", "score": 0.5, "category": "feature", "reasoning": "ok"}]
COMMIT = {"commit_sha": "abc123def456", "title": "feat: auth", "message": "feat: auth"}
DIFFS = [{"commit_sha": "abc123def456", "file_path": "src/auth.py",
          "additions": 50, "deletions": 10, "diff": "+x"}]


def settings(model="claude-haiku-5-5", **kw):
    return scoring.ScoringSettings(model=model, system_prompt="SYS", **kw)


# ── Text extraction ──────────────────────────────────────────────────────────

def test_extract_text_ignores_leading_thinking_block():
    resp = make_response(FILES, thinking_first=True)
    assert resp.content[0].type == "thinking"  # content[0].text would crash
    text = scoring.extract_text(resp)
    assert json.loads(text)["files"][0]["path"] == "src/auth.py"


def test_extract_text_without_text_block_is_none():
    resp = NS(content=[NS(type="thinking", thinking="hmm")], stop_reason="end_turn")
    assert scoring.extract_text(resp) is None


def test_extract_text_works_without_thinking_block():
    assert scoring.extract_text(make_response(FILES, thinking_first=False))


# ── Failure classification ───────────────────────────────────────────────────

@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_refusal_and_max_tokens_are_failed_attempts(stop):
    result, reason = scoring.evaluate_response(make_response(FILES, stop_reason=stop))
    assert result is None and reason == stop


def test_evaluate_response_ok_and_invalid():
    result, reason = scoring.evaluate_response(make_response(FILES))
    assert reason is None and result["files"][0]["score"] == 0.5
    bad = NS(content=[NS(type="text", text="not json")], stop_reason="end_turn")
    assert scoring.evaluate_response(bad) == (None, "invalid_json")


def test_score_commit_retries_after_refusal_and_records_failures():
    client = FakeClient([make_response(FILES, stop_reason="refusal"), make_response(FILES)])
    out = scoring.score_commit(client, settings(), COMMIT, DIFFS, sleep=lambda s: None)
    assert out.result is not None
    assert out.failures == ["refusal"]
    assert out.model == "claude-haiku-5-5-20260301"  # response.model, not the request
    assert out.input_tokens == 200 and out.output_tokens == 100  # both attempts billed
    assert len(client.messages.calls) == 2


def test_score_commit_gives_up_after_max_retries():
    client = FakeClient([make_response(FILES, stop_reason="max_tokens")])
    out = scoring.score_commit(client, settings(), COMMIT, DIFFS, sleep=lambda s: None)
    assert out.result is None
    assert out.failures == ["max_tokens"] * 3


def test_score_commit_api_error_is_a_failed_attempt():
    client = FakeClient([RuntimeError("boom"), make_response(FILES)])
    out = scoring.score_commit(client, settings(), COMMIT, DIFFS, sleep=lambda s: None)
    assert out.result is not None and out.failures == ["error"]


# ── Request shape ────────────────────────────────────────────────────────────

def test_non_haiku4_models_send_adaptive_thinking_and_effort():
    p = settings("claude-haiku-5-5", effort="high", max_tokens=12345).build_params("hi")
    assert p["thinking"] == {"type": "adaptive"}
    assert p["output_config"] == {"effort": "high"}
    assert p["max_tokens"] == 12345
    assert p["model"] == "claude-haiku-5-5"


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-haiku-4-5-20251001"])
def test_haiku4_sends_neither(model):
    p = settings(model).build_params("hi")
    assert "thinking" not in p and "output_config" not in p


def test_every_scoring_call_uses_the_shared_request_builder():
    client = FakeClient([make_response(FILES)])
    scoring.score_commit(client, settings(effort="low"), COMMIT, DIFFS)
    sent = client.messages.calls[0]
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "low"}
    assert sent["system"] == "SYS"


# ── prompt_version ───────────────────────────────────────────────────────────

def test_prompt_version_is_12_hex_and_stable():
    v = settings().prompt_version
    assert len(v) == 12 and int(v, 16) >= 0
    assert v == settings().prompt_version


@pytest.mark.parametrize("change", [
    {"model": "claude-haiku-5-6"}, {"effort": "high"},
])
def test_prompt_version_changes_with_every_instrument_input(change):
    assert settings(**change).prompt_version != settings().prompt_version


def test_prompt_version_changes_with_system_prompt_and_template():
    base = settings().prompt_version
    assert scoring.ScoringSettings("claude-haiku-5-5", "OTHER").prompt_version != base
    args = ("SYS", scoring.USER_PROMPT_TEMPLATE, "claude-haiku-5-5",
            {"type": "adaptive"}, {"effort": "medium"}, scoring.logic_values_fingerprint())
    assert scoring.compute_prompt_version(*args) == base
    assert scoring.compute_prompt_version("SYS", scoring.USER_PROMPT_TEMPLATE + "!", *args[2:]) != base


def test_haiku4_prompt_version_ignores_effort():
    assert (settings("claude-haiku-4-5", effort="low").prompt_version
            == settings("claude-haiku-4-5", effort="max").prompt_version)


# ── Rows carry provenance ────────────────────────────────────────────────────

def test_rows_from_result_carry_model_and_prompt_version():
    rows = scoring.rows_from_result(
        "abc123def456", {"files": FILES, "overall_category": "feature"}, DIFFS,
        "claude-haiku-5-5-20260301", "pv0123456789")
    assert rows["commit_row"]["scorer_model"] == "claude-haiku-5-5-20260301"
    assert rows["commit_row"]["prompt_version"] == "pv0123456789"
    assert rows["file_rows"][0]["scorer_model"] == "claude-haiku-5-5-20260301"
    assert rows["commit_row"]["total_productivity"] == 25.0  # 0.5 * 50 additions


# ── Newer-Haiku check never switches ─────────────────────────────────────────

def test_haiku_version_parsing():
    assert scoring.haiku_version("claude-haiku-4-5-20251001") == (4, 5)
    assert scoring.haiku_version("claude-haiku-5-5") == (5, 5)
    assert scoring.haiku_version("claude-3-5-haiku-20241022") == (3, 5)
    assert scoring.haiku_version("claude-sonnet-5-5") is None


def test_newer_haiku_models():
    avail = ["claude-haiku-4-5-20251001", "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
    assert scoring.newer_haiku_models("claude-haiku-4-5", avail) == ["claude-haiku-5-5"]
    assert scoring.newer_haiku_models("claude-haiku-5-5", avail) == []
    assert scoring.newer_haiku_models("claude-sonnet-5-5", avail) == []


def test_warn_if_newer_haiku_prints_warning_and_returns_without_switching(capsys):
    client = NS(models=NS(list=lambda limit=100: [NS(id="claude-haiku-5-5")]))
    newer = scoring.warn_if_newer_haiku(client, "claude-haiku-4-5")
    out = capsys.readouterr().out
    assert newer == ["claude-haiku-5-5"]
    assert "WARNING" in out and "calibrate" in out


def test_warn_survives_model_listing_failure(capsys):
    def boom(limit=100):
        raise RuntimeError("no network")
    assert scoring.warn_if_newer_haiku(NS(models=NS(list=boom)), "claude-haiku-5-5") == []


# ── Pricing ──────────────────────────────────────────────────────────────────

def test_cost_estimate_and_batch_discount():
    std = scoring.estimate_cost("claude-haiku-5-5", 1_000_000, 1_000_000)
    assert std == pytest.approx(0.60)
    assert scoring.estimate_cost("claude-haiku-5-5", 1_000_000, 1_000_000, batch=True) == pytest.approx(0.30)
    assert scoring.estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0) == pytest.approx(1.0)


def test_unknown_model_has_no_price():
    assert scoring.estimate_cost("claude-mystery-9", 1, 1) is None


# ── Failure log ──────────────────────────────────────────────────────────────

def test_failure_log_reports_per_month():
    log = scoring.FailureLog()
    log.record("2026-01", True, [])
    log.record("2026-01", False, ["refusal", "refusal", "max_tokens"])
    log.record("2026-02", True, ["max_tokens"])
    text = log.report()
    assert "2026-01: 1 commit(s) unscored, 1 scored" in text
    assert "refusal=2" in text and "max_tokens=1" in text
    assert "2026-02" in text
    assert scoring.FailureLog().report().endswith("none.")


# ── Config keys ──────────────────────────────────────────────────────────────

def _config(tmp_dir, body):
    path = tmp_dir / ".coding-productivity.env"
    path.write_text(body)
    path.chmod(0o600)
    return Config(path)


BASE = ("PLATFORM=github\nGITHUB_TOKEN=x\nSTORAGE_BACKEND=duckdb\nDB_PATH=d.duckdb\n"
        "ANONYMIZATION_ENABLED=false\n")


def test_scoring_model_required_when_scoring_enabled(tmp_dir):
    cfg = _config(tmp_dir, BASE + "SCORING_ENABLED=true\nANTHROPIC_API_KEY=k\n")
    assert any("SCORING_MODEL is required" in e for e in cfg.validate())


def test_scoring_defaults_and_overrides(tmp_dir):
    cfg = _config(tmp_dir, BASE + "SCORING_ENABLED=true\nANTHROPIC_API_KEY=k\nSCORING_MODEL=claude-haiku-5-5\n")
    assert cfg.validate() == []
    assert (cfg.SCORING_EFFORT, cfg.SCORING_MAX_TOKENS) == ("medium", 16000)
    cfg = _config(tmp_dir, BASE + "SCORING_ENABLED=true\nANTHROPIC_API_KEY=k\n"
                  "SCORING_MODEL=claude-haiku-5-5\nSCORING_EFFORT=HIGH\nSCORING_MAX_TOKENS=8000\n")
    assert (cfg.SCORING_EFFORT, cfg.SCORING_MAX_TOKENS) == ("high", 8000)


def test_scoring_config_validation_errors(tmp_dir):
    cfg = _config(tmp_dir, BASE + "SCORING_ENABLED=true\nANTHROPIC_API_KEY=k\n"
                  "SCORING_MODEL=m\nSCORING_EFFORT=extreme\nSCORING_MAX_TOKENS=lots\n")
    errors = " ".join(cfg.validate())
    assert "SCORING_EFFORT" in errors and "SCORING_MAX_TOKENS" in errors


def test_scoring_model_not_needed_when_disabled(tmp_dir):
    assert _config(tmp_dir, BASE + "SCORING_ENABLED=false\n").validate() == []
