"""calibrate: statistics, deterministic sampling and the COMPARABLE / REBASE verdict."""

import pytest

from lib import calibration as cal


def records(per_quarter, n=20):
    """Synthetic records. per_quarter: {quarter: (retest_ratio, candidate_ratio)}.

    Every commit has stored productivity 100; retest and candidate totals are
    scaled by the given ratios, so the per-period productivity ratios are exact.
    """
    out = []
    for quarter, (retest_ratio, cand_ratio) in per_quarter.items():
        for i in range(n):
            out.append({
                "quarter": quarter,
                "stored_w": 0.5, "stored_p": 100.0,
                "current_w": 0.5 * retest_ratio, "current_p": 100.0 * retest_ratio,
                "cand_w": 0.5 * cand_ratio, "cand_p": 100.0 * cand_ratio,
            })
    return out


def judge(per_quarter, n=20):
    recs = records(per_quarter, n)
    retest = cal.compare_by_period(recs, "stored", "current")
    cand = cal.compare_by_period(recs, "stored", "cand")
    return cal.verdict(retest, cand)


# ── Verdict logic ────────────────────────────────────────────────────────────

def test_comparable_when_candidate_sits_inside_the_noise_band():
    v = judge({"2026-Q1": (1.03, 1.04), "2026-Q2": (0.97, 1.05)})
    assert v["max_retest_deviation"] == pytest.approx(0.03)
    assert v["band"] == pytest.approx(0.06)
    assert v["verdict"] == "COMPARABLE", v["reasons"]


def test_uniform_shift_outside_the_band_requires_rebase():
    # The motivating case: a constant 0.74x scale shift.
    v = judge({"2026-Q1": (1.02, 0.74), "2026-Q2": (0.98, 0.74)})
    assert v["verdict"] == "REBASE REQUIRED"
    assert "deviates" in v["reasons"][0]


def test_period_dependent_shift_requires_rebase_even_inside_deviation_band():
    # Retest noise 8% -> band 16%. Both ratios are within 10% of 1.0, but they
    # differ by 20% across periods: no single conversion factor exists.
    v = judge({"2026-Q1": (1.08, 0.90), "2026-Q2": (0.92, 1.10)})
    assert v["band"] == pytest.approx(0.16)
    assert v["candidate_max_deviation"] == pytest.approx(0.10)
    assert v["candidate_spread"] == pytest.approx(0.20)
    assert v["verdict"] == "REBASE REQUIRED"
    assert any("spread" in r for r in v["reasons"])


def test_band_has_a_floor():
    # Perfect retest (0 deviation) must not make every candidate fail by construction.
    v = judge({"2026-Q1": (1.0, 1.01), "2026-Q2": (1.0, 0.995)})
    assert v["band"] == cal.MIN_BAND
    assert v["verdict"] == "COMPARABLE"
    assert judge({"2026-Q1": (1.0, 1.05), "2026-Q2": (1.0, 1.05)})["verdict"] == "REBASE REQUIRED"


def test_one_bad_period_is_enough():
    v = judge({"2026-Q1": (1.02, 1.01), "2026-Q2": (0.98, 1.02), "2026-Q3": (1.01, 0.80)})
    assert v["verdict"] == "REBASE REQUIRED"


def test_thin_periods_are_reported_not_judged():
    recs = records({"2026-Q1": (1.0, 1.0)}, n=20) + records({"2026-Q2": (1.0, 0.5)}, n=3)
    v = cal.verdict(cal.compare_by_period(recs, "stored", "current"),
                    cal.compare_by_period(recs, "stored", "cand"))
    assert v["periods_judged"] == ["2026-Q1"]
    assert v["periods_too_thin"] == ["2026-Q2"]
    assert v["verdict"] == "COMPARABLE"


def test_no_judgeable_period_defaults_to_rebase():
    v = judge({"2026-Q1": (1.0, 1.0)}, n=3)
    assert v["verdict"] == "REBASE REQUIRED"
    assert "increase --sample" in v["reasons"][0]


# ── Comparison statistics ────────────────────────────────────────────────────

def test_compare_reports_ratios_and_correlations():
    recs = [{"a_w": w, "a_p": w * 10, "b_w": w * 0.5, "b_p": w * 5}
            for w in (0.1, 0.2, 0.4, 0.8)]
    s = cal.compare(recs, "a", "b")
    assert s["n"] == 4
    assert s["productivity_ratio"] == pytest.approx(0.5)
    assert s["mean_weighted_ratio"] == pytest.approx(0.5)
    assert s["pearson"] == pytest.approx(1.0)
    assert s["spearman"] == pytest.approx(1.0)
    assert s["median_abs_diff"] == pytest.approx((0.1 + 0.2) / 2)  # |w - w/2| median of .05,.1,.2,.4


def test_pearson_and_spearman_known_values():
    assert cal.pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert cal.pearson([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert cal.pearson([1, 1, 1], [1, 2, 3]) is None  # zero variance
    # Monotone but non-linear: Spearman 1, Pearson < 1.
    xs, ys = [1, 2, 3, 4, 5], [1, 4, 9, 16, 100]
    assert cal.spearman(xs, ys) == pytest.approx(1.0)
    assert cal.pearson(xs, ys) < 0.99
    # Ties share average ranks.
    assert cal.spearman([1, 1, 2, 2], [1, 1, 2, 2]) == pytest.approx(1.0)


def test_median():
    assert cal.median([]) is None
    assert cal.median([3, 1, 2]) == 2
    assert cal.median([1, 2, 3, 4]) == 2.5


# ── Sampling ─────────────────────────────────────────────────────────────────

def commits(spec):
    """spec: {month: count} -> commit dicts with unique shas."""
    return [{"commit_sha": f"{m}-{i:03d}", "month": m} for m, c in spec.items() for i in range(c)]


def sample(items, n):
    return cal.stratified_sample(items, n, lambda c: c["month"], lambda c: c["commit_sha"])


def test_sample_is_deterministic_and_input_order_independent():
    items = commits({"2026-01": 50, "2026-02": 30, "2026-03": 20})
    a = sample(items, 30)
    b = sample(list(reversed(items)), 30)
    assert {c["commit_sha"] for c in a} == {c["commit_sha"] for c in b}
    assert len(a) == 30


def test_sample_is_stratified_across_months_proportionally():
    items = commits({"2026-01": 500, "2026-02": 300, "2026-03": 200})
    counts = {}
    for c in sample(items, 100):
        counts[c["month"]] = counts.get(c["month"], 0) + 1
    assert counts == {"2026-01": 50, "2026-02": 30, "2026-03": 20}


def test_every_month_is_represented_when_n_allows():
    items = commits({"2026-01": 1000, "2026-02": 3, "2026-03": 2})
    months = {c["month"] for c in sample(items, 20)}
    assert months == {"2026-01", "2026-02", "2026-03"}


def test_sample_exact_size_and_prefix_stability():
    items = commits({"2026-01": 40, "2026-02": 40, "2026-03": 40})
    small = {c["commit_sha"] for c in sample(items, 30)}
    large = {c["commit_sha"] for c in sample(items, 60)}
    assert len(small) == 30 and len(large) == 60
    assert small <= large  # growing --sample only adds commits


def test_sample_larger_than_population_returns_everything():
    items = commits({"2026-01": 5})
    assert len(sample(items, 300)) == 5
