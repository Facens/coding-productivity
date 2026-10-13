#!/usr/bin/env python3
"""
Compare a candidate scoring model with the current one, without touching data.

Draws a deterministic sample of already-scored commits (stratified across
months) and measures each commit again with

* the CURRENT model (``SCORING_MODEL``): how far does the same model move when
  asked twice? That is the instrument's own noise (test-retest), and
* the CANDIDATE model.

Then reports, overall and per quarter, the ratio of mean weighted_score and of
total_productivity, Pearson and Spearman correlation and the median absolute
difference, and gives a verdict:

* COMPARABLE        - the candidate shifts every period no more than the
                      current model shifts itself (within a 2x band), and
                      the shift is the same in every period.
* REBASE REQUIRED   - otherwise. Mixed-model series inflate trends; run
                      ``rebase.py`` to rescore everything with one model.

Nothing is written to the score tables. Results go to a JSON file.

Usage::

    python calibrate.py --config .coding-productivity.env --candidate claude-haiku-5-5
    python calibrate.py --config .coding-productivity.env --candidate MODEL --sample 150

"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from lib import calibration, scoring, scoring_meta
from lib import schema as _schema
from lib.config import Config
from lib.storage import get_storage


def _make_client(api_key: str):
    try:
        import anthropic
    except ImportError:
        print(
            "Error: the 'anthropic' package is required.\n"
            "Install it with:  pip install anthropic",
            file=sys.stderr,
        )
        sys.exit(1)
    return anthropic.Anthropic(api_key=api_key)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _fetch_scored_commits(storage, commit_table: str, excluded: list[str]) -> list[dict]:
    """Scored commits with diffs, with the stored score and its provenance."""
    _schema.base_table(commit_table)  # whitelist check
    base = (
        "SELECT c.commit_sha, c.title, c.message, c.authored_date, "
        "c.author_email, c.author_name, "
        "s.weighted_score AS stored_w, s.total_productivity AS stored_p"
    )
    tail = (
        f" FROM commits c JOIN {commit_table} s ON c.commit_sha = s.commit_sha "
        "WHERE c.commit_sha IN (SELECT DISTINCT commit_sha FROM diffs)"
    )
    try:
        rows = storage.query(
            base + ", s.scorer_model AS stored_model, s.prompt_version AS stored_pv" + tail
        )
    except Exception as exc:
        if "scorer_model" not in str(exc):
            raise
        rows = storage.query(
            base + ", NULL AS stored_model, NULL AS stored_pv" + tail
        )

    if excluded:
        low = {e.lower() for e in excluded}
        rows = [
            r for r in rows
            if (r.get("author_email") or "").lower() not in low
            and (r.get("author_name") or "").lower() not in low
        ]
    return rows


def _count_eligible(storage, excluded: list[str]) -> int:
    rows = storage.query(
        "SELECT c.author_email, c.author_name FROM commits c "
        "WHERE c.commit_sha IN (SELECT DISTINCT commit_sha FROM diffs)"
    )
    if excluded:
        low = {e.lower() for e in excluded}
        rows = [
            r for r in rows
            if (r.get("author_email") or "").lower() not in low
            and (r.get("author_name") or "").lower() not in low
        ]
    return len(rows)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def _measure(client, settings, commit, diffs):
    """Score one commit; return ``(weighted, productivity, outcome)``."""
    outcome = scoring.score_commit(client, settings, commit, diffs)
    if outcome.result is None:
        return None, None, outcome
    prod = scoring.calculate_productivity(outcome.result, diffs)
    if prod is None:
        return None, None, outcome
    return prod["weighted_score"], prod["total_productivity"], outcome


def _run_pass(client, settings, commits, diffs_by_sha, workers):
    """Measure every commit with *settings*; returns ``{sha: (w, p)}`` + tokens."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda c: _measure(client, settings, c, diffs_by_sha[c["commit_sha"]]),
            commits,
        ))
    measured, tokens_in, tokens_out, failed = {}, 0, 0, 0
    for c, (w, p, outcome) in zip(commits, results):
        tokens_in += outcome.input_tokens
        tokens_out += outcome.output_tokens
        if w is None:
            failed += 1
        else:
            measured[c["commit_sha"]] = (w, p)
    return measured, {"input": tokens_in, "output": tokens_out, "failed": failed}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _table(title: str, by_period: dict[str, dict]) -> str:
    f = calibration.fmt
    lines = [
        title,
        "| period | n | mean w (ref) | mean w (other) | mean w ratio | "
        "productivity ratio | Pearson | Spearman | median abs diff |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for period, s in by_period.items():
        lines.append(
            f"| {period} | {s['n']} | {f(s['mean_weighted_ref'])} | "
            f"{f(s['mean_weighted_other'])} | {f(s['mean_weighted_ratio'])} | "
            f"{f(s['productivity_ratio'])} | {f(s['pearson'])} | "
            f"{f(s['spearman'])} | {f(s['median_abs_diff'])} |"
        )
    return "\n".join(lines)


def _money(value) -> str:
    return "no price known for this model" if value is None else f"${value:,.2f}"


def build_report(current: str, candidate: str, ref_label: str, retest_label: str,
                 retest: dict, cand: dict, cand_vs_current: dict, verdict: dict,
                 tokens: dict, n_sample: int, n_eligible: int,
                 candidate_settings: scoring.ScoringSettings) -> tuple[str, dict]:
    f = calibration.fmt
    lines = [
        f"Calibration: candidate {candidate} vs current {current} "
        f"({n_sample} commits sampled, {n_eligible:,} eligible in total)",
        f"Reference = {ref_label}; retest = {retest_label}.",
        "",
        _table("Test-retest (current model measured twice; the noise floor):", retest),
        "",
        _table(f"Candidate vs reference ({ref_label}):", cand),
        "",
        _table("Candidate vs current-model rescoring (informational):", cand_vs_current),
        "",
        f"Retest max per-period deviation: {f(verdict['max_retest_deviation'])}",
        f"Allowed band (max of 2x retest deviation, floor {calibration.MIN_BAND}): "
        f"{f(verdict['band'])}",
        f"Candidate max per-period deviation: {f(verdict['candidate_max_deviation'])}",
        f"Candidate spread across periods: {f(verdict['candidate_spread'])}",
    ]
    if verdict["periods_too_thin"]:
        lines.append(
            "Not judged (fewer than "
            f"{calibration.MIN_PERIOD_N} commits): {', '.join(verdict['periods_too_thin'])}"
        )
    for reason in verdict["reasons"]:
        lines.append(f"  - {reason}")
    lines += ["", f"VERDICT: {verdict['verdict']}", ""]

    # Tokens and cost
    cur_t, cand_t = tokens["current"], tokens["candidate"]
    lines.append("Tokens used by this calibration:")
    lines.append(f"  current model   {current}: {cur_t['input']:,} in / {cur_t['output']:,} out"
                 f"  (~{_money(scoring.estimate_cost(current, cur_t['input'], cur_t['output']))})")
    lines.append(f"  candidate       {candidate}: {cand_t['input']:,} in / {cand_t['output']:,} out"
                 f"  (~{_money(scoring.estimate_cost(candidate, cand_t['input'], cand_t['output']))})")

    per_commit_in = cand_t["input"] / n_sample if n_sample else 0
    per_commit_out = cand_t["output"] / n_sample if n_sample else 0
    full_in, full_out = per_commit_in * n_eligible, per_commit_out * n_eligible
    batch_cost = scoring.estimate_cost(candidate, full_in, full_out, batch=True)
    sync_cost = scoring.estimate_cost(candidate, full_in, full_out, batch=False)
    lines += [
        "",
        f"Estimated cost of a full rebase of {n_eligible:,} commits with {candidate} "
        f"(extrapolated from the sample: ~{per_commit_in:,.0f} in / "
        f"{per_commit_out:,.0f} out tokens per commit):",
        f"  Batch API (50%): {_money(batch_cost)}",
        f"  Synchronous:     {_money(sync_cost)}",
    ]
    estimate = {
        "eligible_commits": n_eligible,
        "tokens_in_per_commit": per_commit_in,
        "tokens_out_per_commit": per_commit_out,
        "batch_usd": batch_cost,
        "sync_usd": sync_cost,
        "prompt_version": candidate_settings.prompt_version,
    }
    return "\n".join(lines), estimate


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def calibrate(config: Config, candidate: str, sample: int, workers: int,
              output: Path | None, legacy_model: str | None,
              fresh_retest: bool) -> dict:
    scoring.ensure_consent()

    if not config.ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY is not set in config.", file=sys.stderr)
        sys.exit(1)
    client = _make_client(config.ANTHROPIC_API_KEY)

    current_settings = scoring.settings_from_config(config)
    candidate_settings = scoring.settings_from_config(config, model=candidate)
    current = current_settings.model
    if scoring_meta.model_matches(current, candidate) or candidate == current:
        print(f"Warning: the candidate ({candidate}) is the current model; the "
              "result measures noise only.", flush=True)

    # Read-only use of storage: no create_tables(), no writes.
    storage = get_storage(config)
    active = scoring_meta.get_active(storage)
    if active.from_meta and active.scorer_model != current:
        print(f"Error: SCORING_MODEL={current} but the active series "
              f"(v{active.version}) was built with {active.scorer_model}. "
              "Calibrate against the series that is actually in use.", file=sys.stderr)
        sys.exit(1)

    scored = _fetch_scored_commits(storage, active.commit_table, config.EXCLUDED_DEVELOPERS)
    n_eligible = _count_eligible(storage, config.EXCLUDED_DEVELOPERS)
    if not scored:
        print("Error: no scored commits with diffs found. Run /coding-productivity:score first.",
              file=sys.stderr)
        sys.exit(1)

    for c in scored:
        c["month"] = scoring.month_of(c.get("authored_date"))
        c["quarter"] = scoring.quarter_of(c["month"])

    picked = calibration.stratified_sample(
        scored, sample, stratum=lambda c: c["month"], key=lambda c: c["commit_sha"]
    )
    months = {c["month"] for c in picked}
    print(f"Sampled {len(picked)} of {len(scored):,} scored commits across "
          f"{len(months)} month(s) (deterministic, hash-ordered).", flush=True)

    # Provenance: stored scores only serve as reference if the current model made them.
    if not fresh_retest:
        null_rows = [c for c in picked if c.get("stored_model") is None]
        foreign = sorted({
            c["stored_model"] for c in picked
            if c.get("stored_model") is not None
            and not scoring_meta.model_matches(current, c["stored_model"])
        })
        if foreign:
            print(f"Error: sampled scores were produced by {', '.join(foreign)}, not by "
                  f"SCORING_MODEL={current}, so they cannot serve as the test-retest "
                  "reference. Re-run with --fresh-retest (a second pass with the current "
                  "model, costs one more call per commit).", file=sys.stderr)
            sys.exit(1)
        # Same model but another prompt, effort or max_tokens is another
        # instrument too: it would inflate the noise band toward COMPARABLE.
        current_pv = current_settings.prompt_version
        other_pv = sorted({
            c["stored_pv"] for c in picked
            if c.get("stored_pv") is not None and c["stored_pv"] != current_pv
        })
        if other_pv:
            print(f"Error: sampled scores were produced with prompt_version "
                  f"{', '.join(other_pv)}, but the current settings give {current_pv} "
                  "(the prompt, SCORING_EFFORT or SCORING_MAX_TOKENS changed). They "
                  "cannot serve as the test-retest reference. Re-run with "
                  "--fresh-retest.", file=sys.stderr)
            sys.exit(1)
        if null_rows:
            if not legacy_model:
                print(f"Error: {len(null_rows)} sampled score(s) have no recorded model "
                      "(written before provenance existed). Pass --legacy-model MODEL, "
                      "the model that produced them (it must equal SCORING_MODEL), or "
                      "use --fresh-retest.", file=sys.stderr)
                sys.exit(1)
            if not scoring_meta.model_matches(current, legacy_model) and legacy_model != current:
                print(f"Error: --legacy-model {legacy_model} differs from "
                      f"SCORING_MODEL={current}. Legacy scores from another model cannot "
                      "serve as the test-retest reference; use --fresh-retest.",
                      file=sys.stderr)
                sys.exit(1)

    # Diffs on the main thread (DuckDB connections are not thread-safe).
    diffs_by_sha = {
        c["commit_sha"]: storage.query(
            "SELECT * FROM diffs WHERE commit_sha = $sha", {"sha": c["commit_sha"]}
        )
        for c in picked
    }
    storage.close()

    print(f"Scoring with the current model ({current})...", flush=True)
    cur_pass, cur_tokens = _run_pass(client, current_settings, picked, diffs_by_sha, workers)
    retest_pass, retest_tokens = None, {"input": 0, "output": 0, "failed": 0}
    if fresh_retest:
        print(f"Second pass with the current model ({current}) for the noise floor...", flush=True)
        retest_pass, retest_tokens = _run_pass(client, current_settings, picked, diffs_by_sha, workers)
    print(f"Scoring with the candidate ({candidate})...", flush=True)
    cand_pass, cand_tokens = _run_pass(client, candidate_settings, picked, diffs_by_sha, workers)

    # Keep only commits measured by every pass, so all tables use the same set.
    records = []
    for c in picked:
        sha = c["commit_sha"]
        if sha not in cur_pass or sha not in cand_pass:
            continue
        if fresh_retest and sha not in retest_pass:
            continue
        rec = {
            "commit_sha": sha, "month": c["month"], "quarter": c["quarter"],
            "stored_model": c.get("stored_model"),
            "stored_w": c["stored_w"], "stored_p": c["stored_p"],
            "current_w": cur_pass[sha][0], "current_p": cur_pass[sha][1],
            "cand_w": cand_pass[sha][0], "cand_p": cand_pass[sha][1],
        }
        if fresh_retest:
            rec["retest_w"], rec["retest_p"] = retest_pass[sha]
        records.append(rec)

    dropped = len(picked) - len(records)
    if dropped:
        print(f"Note: {dropped} sampled commit(s) failed in at least one pass and are excluded.")
    if not records:
        print("Error: no commit could be measured by all passes.", file=sys.stderr)
        sys.exit(1)

    if fresh_retest:
        ref, retest_key = "current", "retest"
        ref_label, retest_label = f"fresh {current} rescoring", f"second fresh {current} rescoring"
    else:
        ref, retest_key = "stored", "current"
        ref_label, retest_label = f"stored scores ({current})", f"{current} rescoring"

    retest_cmp = calibration.compare_by_period(records, ref, retest_key)
    cand_cmp = calibration.compare_by_period(records, ref, "cand")
    cand_vs_cur = calibration.compare_by_period(records, "current", "cand")
    verdict = calibration.verdict(retest_cmp, cand_cmp)

    tokens = {
        "current": {k: cur_tokens[k] + retest_tokens[k] for k in ("input", "output")},
        "candidate": {k: cand_tokens[k] for k in ("input", "output")},
    }
    # The cost extrapolation is per candidate call, so use the candidate pass alone.
    report, estimate = build_report(
        current, candidate, ref_label, retest_label, retest_cmp, cand_cmp,
        cand_vs_cur, verdict, tokens, len(records), n_eligible, candidate_settings,
    )
    print()
    print(report)

    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "current_model": current,
        "candidate_model": candidate,
        "current_prompt_version": current_settings.prompt_version,
        "candidate_prompt_version": candidate_settings.prompt_version,
        "mode": "fresh-retest" if fresh_retest else "stored-vs-rescore",
        "legacy_model": legacy_model,
        "sample_requested": sample,
        "sample_measured": len(records),
        "retest": retest_cmp,
        "candidate_vs_reference": cand_cmp,
        "candidate_vs_current_rescore": cand_vs_cur,
        "verdict": verdict,
        "tokens": tokens,
        "rebase_estimate": estimate,
        "records": records,
    }
    if output is None:
        safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in candidate)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = Path(config._path).parent / ".coding-productivity" / f"calibration_{safe}_{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"Results written to {output}")
    print(f"VERDICT={verdict['verdict'].replace(' ', '_')}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare a candidate scoring model with the current one (no writes to score tables)."
    )
    parser.add_argument("--config", "-c", default=None,
                        help="Path to .coding-productivity.env (auto-detected if omitted).")
    parser.add_argument("--candidate", required=True, help="Model ID to evaluate.")
    parser.add_argument("--sample", type=int, default=300,
                        help="Commits to sample, stratified across months (default: 300).")
    parser.add_argument("--workers", "-w", type=int, default=10,
                        help="Parallel API workers (default: 10).")
    parser.add_argument("--output", "-o", default=None,
                        help="JSON results file (default: .coding-productivity/calibration_<model>_<time>.json).")
    parser.add_argument("--legacy-model", default=None,
                        help="Model that produced stored scores with no recorded model. "
                             "Must equal SCORING_MODEL.")
    parser.add_argument("--fresh-retest", action="store_true",
                        help="Measure the noise floor with a second fresh pass of the current "
                             "model instead of the stored scores (one extra call per commit).")
    args = parser.parse_args()

    config = Config(args.config) if args.config else Config()
    errors = config.validate()
    if errors:
        for e in errors:
            print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)
    if not config.SCORING_ENABLED:
        print("Scoring is disabled. Set SCORING_ENABLED=true in your config.", file=sys.stderr)
        sys.exit(1)
    if args.sample < 1:
        print("Error: --sample must be at least 1.", file=sys.stderr)
        sys.exit(1)

    calibrate(
        config, args.candidate, args.sample, args.workers,
        Path(args.output) if args.output else None,
        args.legacy_model, args.fresh_retest,
    )


if __name__ == "__main__":
    main()
