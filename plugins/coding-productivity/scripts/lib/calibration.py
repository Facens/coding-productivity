"""
Pure math for ``calibrate``: sampling, correlation, per-period ratios, verdict.

Kept free of I/O and of the Anthropic SDK so the verdict logic is testable on
synthetic numbers.

Vocabulary
----------
* *reference*  - the series the candidate is judged against (the stored scores,
  or a fresh rescoring with the current model).
* *retest*     - a second measurement by the CURRENT model of the same commits.
  How far retest drifts from the reference is the instrument's own noise.
* *candidate*  - the same commits scored by the model under evaluation.

A candidate is COMPARABLE only if it moves the per-period productivity totals
no more than the current model moves them by itself (with a safety factor).
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Callable, Iterable, Optional

BAND_FACTOR = 2.0   # candidate may deviate up to 2x the worst retest deviation
MIN_BAND = 0.02     # floor so a lucky, near-zero retest deviation cannot make
                    # every candidate fail by construction
MIN_PERIOD_N = 10   # periods thinner than this are reported, not judged


# ── Basic statistics ─────────────────────────────────────────────────────────

def median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation; None when undefined (n < 2 or zero variance)."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _ranks(values: list[float]) -> list[float]:
    """1-based ranks, ties share their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    return pearson(_ranks(xs), _ranks(ys))


# ── Sampling ─────────────────────────────────────────────────────────────────

def stable_hash(text: str) -> str:
    """Deterministic ordering key (``hash()`` is salted per process)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stratified_sample(items: list[dict], n: int,
                      stratum: Callable[[dict], str],
                      key: Callable[[dict], str]) -> list[dict]:
    """Deterministic sample of up to *n* items, proportional across strata.

    Items are ordered by ``stable_hash(key(item))`` inside each stratum, so the
    same data and n always give the same sample, and a smaller n is a prefix of
    a larger one per stratum. Every non-empty stratum gets at least one item
    when n allows; allocation is otherwise proportional (largest remainder).
    """
    if n >= len(items):
        return sorted(items, key=lambda it: (stratum(it), stable_hash(key(it))))

    buckets: dict[str, list[dict]] = {}
    for it in items:
        buckets.setdefault(stratum(it), []).append(it)
    for b in buckets.values():
        b.sort(key=lambda it: stable_hash(key(it)))

    names = sorted(buckets)
    total = len(items)
    quotas = {s: n * len(buckets[s]) / total for s in names}
    alloc = {s: int(quotas[s]) for s in names}
    if n >= len(names):
        for s in names:
            alloc[s] = max(alloc[s], 1)
    # Largest remainder fills what is left; trim the largest strata on overshoot.
    left = n - sum(alloc.values())
    by_remainder = sorted(names, key=lambda s: (-(quotas[s] - int(quotas[s])), s))
    i = 0
    while left > 0:
        s = by_remainder[i % len(by_remainder)]
        if alloc[s] < len(buckets[s]):
            alloc[s] += 1
            left -= 1
        i += 1
        if i > 10 * len(names) + n:
            break
    while left < 0:
        s = max(names, key=lambda s: (alloc[s], s))
        if alloc[s] <= 1 and n >= len(names):
            break
        alloc[s] -= 1
        left += 1

    picked: list[dict] = []
    for s in names:
        picked.extend(buckets[s][: min(alloc[s], len(buckets[s]))])
    return picked


# ── Per-period comparison ────────────────────────────────────────────────────

def compare(records: list[dict], ref: str, other: str) -> dict:
    """Compare two measurements of the same commits.

    Each record holds ``{ref}_w`` / ``{ref}_p`` (weighted_score and
    total_productivity) and the same for *other*. Records missing either side
    are skipped. Ratios are other / ref.
    """
    rows = [
        r for r in records
        if all(r.get(f"{side}_{m}") is not None for side in (ref, other) for m in ("w", "p"))
    ]
    rw = [r[f"{ref}_w"] for r in rows]
    ow = [r[f"{other}_w"] for r in rows]
    rp = sum(r[f"{ref}_p"] for r in rows)
    op = sum(r[f"{other}_p"] for r in rows)
    mean_ref = sum(rw) / len(rw) if rw else None
    mean_other = sum(ow) / len(ow) if ow else None
    return {
        "n": len(rows),
        "mean_weighted_ref": mean_ref,
        "mean_weighted_other": mean_other,
        "mean_weighted_ratio": (mean_other / mean_ref) if mean_ref else None,
        "total_productivity_ref": rp,
        "total_productivity_other": op,
        "productivity_ratio": (op / rp) if rp else None,
        "pearson": pearson(rw, ow),
        "spearman": spearman(rw, ow),
        "median_abs_diff": median([abs(a - b) for a, b in zip(rw, ow)]),
    }


def compare_by_period(records: list[dict], ref: str, other: str,
                      period_key: str = "quarter") -> dict[str, dict]:
    """``{"overall": ..., "<period>": ...}`` comparison tables."""
    out = {"overall": compare(records, ref, other)}
    for period in sorted({r[period_key] for r in records}):
        out[period] = compare([r for r in records if r[period_key] == period], ref, other)
    return out


# ── Verdict ──────────────────────────────────────────────────────────────────

def _usable_ratios(by_period: dict[str, dict], min_n: int) -> dict[str, float]:
    return {
        p: s["productivity_ratio"]
        for p, s in by_period.items()
        if p != "overall" and s["n"] >= min_n and s["productivity_ratio"] is not None
    }


def verdict(retest: dict[str, dict], candidate: dict[str, dict],
            band_factor: float = BAND_FACTOR, min_band: float = MIN_BAND,
            min_n: int = MIN_PERIOD_N) -> dict[str, Any]:
    """COMPARABLE or REBASE REQUIRED.

    *retest* and *candidate* are ``compare_by_period`` outputs. With

    * ``max_dev``  = max over periods of |retest ratio - 1|  (the instrument's
      own noise),
    * ``band``     = max(band_factor * max_dev, min_band),

    the candidate is COMPARABLE only if every per-period productivity ratio
    satisfies |ratio - 1| <= band AND the spread of the per-period ratios
    (max - min) is <= band. Anything else, including too little data to judge,
    is REBASE REQUIRED: the default is the safe one.
    """
    retest_ratios = _usable_ratios(retest, min_n)
    cand_ratios = _usable_ratios(candidate, min_n)
    periods = sorted(set(retest_ratios) & set(cand_ratios))

    result: dict[str, Any] = {
        "verdict": "REBASE REQUIRED",
        "periods_judged": periods,
        "periods_too_thin": sorted(
            p for p in set(retest) | set(candidate)
            if p != "overall" and p not in periods
        ),
        "max_retest_deviation": None,
        "band": None,
        "candidate_max_deviation": None,
        "candidate_spread": None,
        "reasons": [],
    }
    if not periods:
        result["reasons"].append(
            f"no period has at least {min_n} commits scored by both passes; "
            "increase --sample"
        )
        return result

    max_dev = max(abs(retest_ratios[p] - 1) for p in periods)
    band = max(band_factor * max_dev, min_band)
    ratios = [cand_ratios[p] for p in periods]
    cand_dev = max(abs(r - 1) for r in ratios)
    spread = max(ratios) - min(ratios)

    result.update(
        max_retest_deviation=max_dev, band=band,
        candidate_max_deviation=cand_dev, candidate_spread=spread,
    )
    if cand_dev > band:
        worst = max(periods, key=lambda p: abs(cand_ratios[p] - 1))
        result["reasons"].append(
            f"period {worst} ratio {cand_ratios[worst]:.3f} deviates "
            f"{cand_dev:.3f} from 1.0, more than the band {band:.3f}"
        )
    if spread > band:
        result["reasons"].append(
            f"per-period ratios spread {spread:.3f} (min {min(ratios):.3f}, "
            f"max {max(ratios):.3f}), more than the band {band:.3f}: no single "
            "conversion factor would fix it"
        )
    if not result["reasons"]:
        result["verdict"] = "COMPARABLE"
    return result


def fmt(value: Optional[float], digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"
