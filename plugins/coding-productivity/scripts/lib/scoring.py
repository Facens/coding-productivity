"""
Shared commit-scoring logic: prompt, request shape, response handling, rows.

Used by ``score_commits.py`` (synchronous scoring), ``calibrate.py`` (candidate
model comparison) and ``rebase.py`` (Message Batches rescoring). Everything that
defines the *instrument* (the model, how it is asked, how its answer is read)
lives here so the three paths cannot drift apart.

This module does not import the ``anthropic`` package at import time, so the
pure helpers stay testable without the SDK installed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .scoring_meta import model_matches

try:  # optional dependency
    import anthropic
except ImportError:  # pragma: no cover - exercised only without the SDK
    anthropic = None

_SCRIPT_DIR = Path(__file__).resolve().parent.parent

CONSENT_FILE = Path(".coding-productivity") / ".scoring_consent"

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TOKENS = 16000
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

VALID_CATEGORIES = {
    "feature", "bugfix", "refactor", "test", "docs", "style",
    "perf", "security", "deps", "config", "chore", "localization",
}

# Files auto-scored without an API call (pattern -> score).
AUTO_SCORE_PATTERNS: dict[str, float] = {
    # Lockfiles
    r".*\.lock$": 0.0,
    r"yarn\.lock$": 0.0,
    r"Gemfile\.lock$": 0.0,
    r"package-lock\.json$": 0.0,
    r"pnpm-lock\.yaml$": 0.0,
    r"composer\.lock$": 0.0,
    r"Pipfile\.lock$": 0.0,
    r"poetry\.lock$": 0.0,
    r"Cargo\.lock$": 0.0,
    # Minified files
    r".*\.min\.js$": 0.0,
    r".*\.min\.css$": 0.0,
    # Source maps
    r".*\.map$": 0.02,
    # Generated / build output
    r"^generated/.*": 0.0,
    r"^dist/.*": 0.0,
    r"^build/.*": 0.0,
    r".*/generated/.*": 0.0,
    r".*/dist/.*": 0.0,
    r".*/build/.*": 0.0,
    # Localization / translation files
    r".*/locales/.*\.yml$": 0.05,
    r".*/locales/.*\.yaml$": 0.05,
    r".*/i18n/.*\.yml$": 0.05,
    r".*/i18n/.*\.json$": 0.05,
    r".*/translations/.*": 0.05,
    # Config
    r"\.gitignore$": 0.1,
}

# Post-processing score caps by extension.
MAX_SCORE_BY_EXTENSION: dict[str, float] = {
    ".md": 0.15,
}

# Large JSON files (> threshold lines, excluding package.json) are capped.
LARGE_JSON_THRESHOLD = 500
LARGE_JSON_MAX_SCORE = 0.05

# Diff truncation per attempt: later attempts send less.
DIFF_SIZES = [10_000, 2_000, 1_000]

# Cut lengths in the prompt and in stored text.
MESSAGE_MAX_CHARS = 500
REASONING_MAX_CHARS = 1000

# Bump when post-processing CODE changes behaviour in a way the values in
# ``logic_values_fingerprint`` cannot see (apply_score_caps, how scores are
# aggregated, normalization). tests/test_scoring.py pins golden outputs and
# the fingerprint so a silent change fails the suite.
SCORING_LOGIC_VERSION = 1

# Stop reasons that mean "no usable answer", even when the call succeeded.
FAILED_STOP_REASONS = ("refusal", "max_tokens")

# The user prompt. Part of the instrument definition: it is hashed into
# ``prompt_version``, so changing it here starts a new series.
USER_PROMPT_TEMPLATE = (
    "Analyze this commit and score EACH FILE separately.\n\n"
    "**Commit Message:**\n$title\n\n"
    "**Full Message:**\n$message\n\n"
    "**Files Changed:**\n$file_list\n\n"
    "**Diffs by File:**\n$diffs\n\n"
    "Respond with ONLY valid JSON (no markdown, no code blocks, just raw JSON):\n"
    '{\n  "files": [\n    {\n      "path": "path/to/file.js",\n'
    '      "score": 0.5,\n      "category": "feature",\n'
    '      "reasoning": "brief explanation"\n    }\n  ],\n'
    '  "overall_category": "feature",\n  "flags": []\n}'
)
_USER_TEMPLATE = string.Template(USER_PROMPT_TEMPLATE)


# ── Prompt ───────────────────────────────────────────────────────────────────

def load_scoring_prompt() -> str:
    """Read the scoring rubric shipped in references/.

    A missing file is an error, never a silent fallback: a different prompt is
    a different instrument, and its scores would join the series unnoticed.
    """
    prompt_path = _SCRIPT_DIR.parent / "references" / "commit_scoring_prompt.md"
    if not prompt_path.is_file():
        raise FileNotFoundError(
            f"Scoring prompt not found at {prompt_path}. Reinstall the plugin; "
            "scoring will not run with a substitute prompt."
        )
    return prompt_path.read_text(encoding="utf-8")


def auto_score_file(file_path: str) -> float | None:
    """Return a fixed score if the file matches a trivial pattern, else None."""
    for pattern, score in AUTO_SCORE_PATTERNS.items():
        if re.match(pattern, file_path, re.IGNORECASE):
            return score
    return None


def apply_score_caps(file_path: str, score: float, lines: int) -> float:
    """Cap scores based on file type and size."""
    _, ext = os.path.splitext(file_path.lower())

    if ext in MAX_SCORE_BY_EXTENSION:
        score = min(score, MAX_SCORE_BY_EXTENSION[ext])

    if ext == ".json" and "package.json" not in file_path.lower():
        if lines > LARGE_JSON_THRESHOLD:
            score = min(score, LARGE_JSON_MAX_SCORE)

    return score


def truncate_diff(diff_content: str | None, max_chars: int = 10_000) -> str:
    if not diff_content:
        return ""
    if len(diff_content) <= max_chars:
        return diff_content
    return diff_content[:max_chars] + "\n... [truncated]"


def build_user_prompt(commit: dict, diffs: list[dict], diff_max: int = DIFF_SIZES[0]) -> str:
    """Render the user prompt for one commit."""
    file_list_lines = []
    parts = []
    for d in diffs:
        adds = d.get("additions") or 0
        dels = d.get("deletions") or 0
        fp = d["file_path"]
        file_list_lines.append(f"- {fp} (+{adds}/-{dels})")

        auto = auto_score_file(fp)
        if auto is not None:
            parts.append(f"### {fp}\n[Auto-scored: {auto} - lockfile/generated]")
        else:
            trunc = truncate_diff(d.get("diff"), diff_max)
            parts.append(f"### {fp}\n```diff\n{trunc}\n```")

    return _USER_TEMPLATE.substitute(
        title=commit.get("title", "") or "",
        message=(commit.get("message") or commit.get("title", "") or "")[:MESSAGE_MAX_CHARS],
        file_list="\n".join(file_list_lines),
        diffs="\n".join(parts),
    )


# ── Instrument definition ────────────────────────────────────────────────────

# Models known to accept ``thinking={"type": "adaptive"}`` together with
# ``output_config.effort``. Matched as an exact ID or ID plus ``-<suffix>``
# (dated snapshots). Everything else is rejected at startup rather than
# failing with 400s in the middle of a rebase.
ADAPTIVE_MODEL_PREFIXES = (
    "claude-haiku-5", "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-fable", "claude-mythos",
)
# Known models that take neither parameter: send neither.
NO_THINKING_MODEL_PREFIXES = ("claude-haiku-4", "claude-3")


class UnsupportedModelError(ValueError):
    """The model is not on the allowlist of models the plugin knows how to ask."""


def _has_prefix(model: str, prefix: str) -> bool:
    return model == prefix or model.startswith(prefix + "-")


def model_kind(model: str) -> Optional[str]:
    """``"adaptive"``, ``"legacy"`` (no thinking/effort) or None (unknown)."""
    m = (model or "").lower()
    if any(_has_prefix(m, p) for p in NO_THINKING_MODEL_PREFIXES):
        return "legacy"
    if any(_has_prefix(m, p) for p in ADAPTIVE_MODEL_PREFIXES):
        return "adaptive"
    return None


def supports_adaptive_thinking(model: str) -> bool:
    return model_kind(model) == "adaptive"


def check_supported_model(model: str) -> None:
    if model_kind(model) is None:
        raise UnsupportedModelError(
            f"Unknown scoring model '{model}'. Supported: Haiku 4.x (no thinking), "
            "and Haiku 5.x, Sonnet 4.6/5.x, Opus 4.6+/5.x, Fable, Mythos "
            "(adaptive thinking). Add the model family to ADAPTIVE_MODEL_PREFIXES "
            "in scripts/lib/scoring.py once it is known to accept "
            "thinking={'type': 'adaptive'} and output_config.effort."
        )


def logic_values_fingerprint() -> str:
    """Hash of the post-processing constants (and ``SCORING_LOGIC_VERSION``).

    These shape every stored score but are not part of the request, so they
    are folded into ``prompt_version`` separately.
    """
    payload = json.dumps(
        {
            "logic_version": SCORING_LOGIC_VERSION,
            "auto_score_patterns": AUTO_SCORE_PATTERNS,
            "max_score_by_extension": MAX_SCORE_BY_EXTENSION,
            "large_json": [LARGE_JSON_THRESHOLD, LARGE_JSON_MAX_SCORE],
            "diff_sizes": DIFF_SIZES,
            "message_max_chars": MESSAGE_MAX_CHARS,
            "reasoning_max_chars": REASONING_MAX_CHARS,
            "valid_categories": sorted(VALID_CATEGORIES),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ScoringSettings:
    """Everything that defines the measuring instrument."""

    model: str
    system_prompt: str
    effort: str = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self) -> None:
        check_supported_model(self.model)

    @property
    def thinking(self) -> Optional[dict]:
        return {"type": "adaptive"} if supports_adaptive_thinking(self.model) else None

    @property
    def output_config(self) -> Optional[dict]:
        if not supports_adaptive_thinking(self.model):
            return None
        return {"effort": self.effort}

    @property
    def prompt_version(self) -> str:
        return compute_prompt_version(
            self.system_prompt, USER_PROMPT_TEMPLATE, self.model,
            self.thinking, self.output_config, logic_values_fingerprint(),
        )

    def build_params(self, user_prompt: str) -> dict:
        """Request body for one commit. Shared by sync calls and batches."""
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if self.thinking is not None:
            params["thinking"] = dict(self.thinking)
        if self.output_config is not None:
            params["output_config"] = dict(self.output_config)
        return params

    def with_model(self, model: str) -> "ScoringSettings":
        return ScoringSettings(model, self.system_prompt, self.effort, self.max_tokens)


def settings_from_config(config, model: Optional[str] = None) -> ScoringSettings:
    return ScoringSettings(
        model=model or config.SCORING_MODEL,
        system_prompt=load_scoring_prompt(),
        effort=config.SCORING_EFFORT,
        max_tokens=config.SCORING_MAX_TOKENS,
    )


def settings_or_exit(config, model: Optional[str] = None) -> ScoringSettings:
    """``settings_from_config`` for CLI entry points: a clear message and exit
    code 1 instead of a traceback (unknown model, missing prompt file)."""
    import sys
    try:
        return settings_from_config(config, model)
    except (UnsupportedModelError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def compute_prompt_version(system_prompt: str, user_template: str, model: str,
                           thinking: Optional[dict], output_config: Optional[dict],
                           logic: str) -> str:
    """First 12 hex of sha256 over everything that defines the instrument.

    ``max_tokens`` is deliberately not part of it: it is a failure-handling
    knob (raising it rescues truncated answers) and must be changeable in the
    middle of a rebase.
    """
    payload = json.dumps(
        {
            "system": system_prompt,
            "user_template": user_template,
            "model": model,
            "thinking": thinking,
            "effort": (output_config or {}).get("effort"),
            "logic": logic,
        },
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# ── Response handling ────────────────────────────────────────────────────────

def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_text(response: Any) -> Optional[str]:
    """Text of the first ``text`` block. 5.x models return a thinking block
    first, so ``content[0]`` is not safe."""
    for block in _get(response, "content", None) or []:
        if _get(block, "type") == "text":
            return _get(block, "text", "") or ""
    return None


def parse_json_response(text: str) -> dict | None:
    """Extract a JSON object from the model's response."""
    clean = text.strip()

    # Strip markdown code fences.
    if "```" in clean:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean)
        if m:
            clean = m.group(1)
        else:
            clean = clean.replace("```json", "").replace("```", "")
    clean = clean.strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{[\s\S]*\}", clean)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    return None


def _flatten_flags(value: Any) -> list[str]:
    """Flags as a flat list of strings; anything else is dropped (models have
    returned nested lists, which BigQuery rejects)."""
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        out.extend(_flatten_flags(item))
    return out


def _entry_score(entry: Any) -> float | None:
    """Finite float score of a well-formed file entry, else None."""
    if not isinstance(entry, dict):
        return None
    path, raw = entry.get("path"), entry.get("score")
    if not isinstance(path, str) or not path or isinstance(raw, bool):
        return None
    try:
        score = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return score if math.isfinite(score) else None


def normalize_score_result(result: Any) -> dict | None:
    """Validate and normalize the model's JSON; None if it breaks the contract.

    ``files`` must be a list of objects with a non-empty string ``path`` and a
    numeric, finite ``score`` (clamped to [0, 1]). An entry that breaks that is
    dropped and counted in ``dropped_files`` (the rest of the commit is kept).
    If ``files`` itself is malformed or no valid entry remains, the answer
    cannot be trusted and callers treat it as ``invalid_json`` (retryable).
    Free-text fields are coerced; ``flags`` is flattened to strings.
    """
    if not isinstance(result, dict):
        return None
    files = result.get("files")
    if not isinstance(files, list):
        return None

    normalized: list[dict] = []
    dropped = 0
    for f in files:
        # One bad entry costs that file, not the commit.
        score = _entry_score(f)
        if score is None:
            dropped += 1
            continue
        path = f["path"]
        category = f.get("category")
        reasoning = f.get("reasoning")
        normalized.append({
            "path": path,
            "score": min(1.0, max(0.0, score)),
            "category": category if isinstance(category, str) else "unknown",
            "reasoning": reasoning if isinstance(reasoning, str) else "",
        })

    if not normalized:
        return None  # nothing usable: the answer cannot be trusted
    overall = result.get("overall_category")
    return {
        "files": normalized,
        "dropped_files": dropped,
        "overall_category": overall[:64] if isinstance(overall, str) else "unknown",
        "flags": _flatten_flags(result.get("flags")),
    }


def evaluate_response(response: Any, expected_model: str | None = None
                      ) -> tuple[dict | None, str | None]:
    """Turn a Messages response into ``(score_result, failure_reason)``.

    A refusal or a ``max_tokens`` stop is a failed attempt even though the
    call itself succeeded: the text, if any, is incomplete or absent. So is an
    answer from a different model than *expected_model* (``model_mismatch``)
    and one that breaks the JSON contract (``invalid_json``).
    """
    stop = _get(response, "stop_reason")
    if stop in FAILED_STOP_REASONS:
        return None, stop
    if expected_model is not None and not model_matches(expected_model, _get(response, "model")):
        return None, "model_mismatch"
    text = extract_text(response)
    if text is None:
        return None, "no_text"
    result = normalize_score_result(parse_json_response(text))
    if result is None:
        return None, "invalid_json"
    return result, None


@dataclass
class ScoreOutcome:
    """Result of scoring one commit, including what it cost and what failed."""

    result: dict | None = None
    model: str | None = None  # response.model of the successful call
    input_tokens: int = 0
    output_tokens: int = 0
    failures: list[str] = field(default_factory=list)  # one reason per failed attempt
    diff_chars: int | None = None  # per-file diff cap of the successful attempt
    dropped_files: int = 0  # malformed file entries dropped from the answer


def _add_usage(outcome: ScoreOutcome, response: Any) -> None:
    usage = _get(response, "usage")
    if usage is not None:
        outcome.input_tokens += _get(usage, "input_tokens", 0) or 0
        outcome.output_tokens += _get(usage, "output_tokens", 0) or 0


def score_commit(client: Any, settings: ScoringSettings, commit: dict,
                 diffs: list[dict], max_retries: int = 3,
                 sleep=None) -> ScoreOutcome:
    """Score every file of one commit with one synchronous Messages call
    (retrying on failures, sending smaller diffs each time)."""
    outcome = ScoreOutcome()
    sleep = sleep or time.sleep

    for attempt in range(max_retries):
        diff_max = DIFF_SIZES[min(attempt, len(DIFF_SIZES) - 1)]
        params = settings.build_params(build_user_prompt(commit, diffs, diff_max))

        try:
            response = client.messages.create(**params)
        except Exception as exc:  # noqa: BLE001 - classified below
            if anthropic is not None and isinstance(exc, anthropic.RateLimitError):
                outcome.failures.append("rate_limit")
                sleep((2 ** attempt) * 2)
                continue
            if anthropic is not None and isinstance(exc, anthropic.APIError):
                outcome.failures.append("api_error")
            else:
                outcome.failures.append("error")
        else:
            _add_usage(outcome, response)
            result, reason = evaluate_response(response, settings.model)
            if result is not None:
                outcome.result = result
                outcome.model = _get(response, "model")
                outcome.diff_chars = diff_max
                outcome.dropped_files = result.get("dropped_files", 0)
                return outcome
            outcome.failures.append(reason or "invalid_json")

        if attempt < max_retries - 1:
            sleep(0.5)

    return outcome


# ── Productivity and rows ────────────────────────────────────────────────────

def calculate_productivity(score_result: dict, diffs: list[dict]) -> dict | None:
    """Aggregate per-file scores into commit-level productivity metrics."""
    score_result = normalize_score_result(score_result)
    if score_result is None:
        return None

    diff_lines: dict[str, dict] = {}
    for d in diffs:
        adds = d.get("additions") or 0
        dels = d.get("deletions") or 0
        diff_lines[d["file_path"]] = {
            "additions": adds,
            "deletions": dels,
            "total": adds + dels,
        }

    total_productivity = 0.0
    total_lines = 0
    file_scores: list[dict] = []

    for fs in score_result["files"]:
        path = fs["path"]
        if path not in diff_lines:
            continue

        info = diff_lines[path]
        lines = info["total"]
        score = apply_score_caps(path, fs["score"], lines)

        # productivity = score * max(additions, 1)
        total_productivity += score * max(info["additions"], 1)
        total_lines += lines

        cat = fs["category"]
        if cat not in VALID_CATEGORIES:
            cat = "chore"

        file_scores.append({
            "commit_sha": "",  # filled in by caller
            "file_path": path,
            "score": round(score, 4),
            "category": cat,
            "reasoning": fs["reasoning"][:REASONING_MAX_CHARS],
            "scored_at": "",  # filled in by caller
        })

    weighted_score = total_productivity / total_lines if total_lines > 0 else 0.0

    return {
        "files": file_scores,
        "total_productivity": round(total_productivity, 4),
        "weighted_score": round(weighted_score, 4),
        "total_lines": total_lines,
        "overall_category": score_result["overall_category"],
    }


def rows_from_result(sha: str, score_result: dict, diffs: list[dict],
                     scorer_model: str | None, prompt_version: str,
                     diff_chars: int | None = None) -> dict | None:
    """Build the storage rows for one scored commit, provenance included.

    Returns ``{"commit_row", "file_rows", "total_lines"}`` or None if the
    result cannot be aggregated.
    """
    prod = calculate_productivity(score_result, diffs)
    if not prod:
        return None

    scored_at = datetime.now(timezone.utc).isoformat()
    file_rows = []
    for f in prod["files"]:
        f["commit_sha"] = sha
        f["scored_at"] = scored_at
        f["scorer_model"] = scorer_model
        f["prompt_version"] = prompt_version
        file_rows.append(f)

    return {
        "commit_row": {
            "commit_sha": sha,
            "total_productivity": prod["total_productivity"],
            "weighted_score": prod["weighted_score"],
            "overall_category": prod["overall_category"],
            "scored_at": scored_at,
            "scorer_model": scorer_model,
            "prompt_version": prompt_version,
            "diff_chars": diff_chars,
        },
        "file_rows": file_rows,
        "total_lines": prod["total_lines"],
    }


def score_and_build_rows(client: Any, settings: ScoringSettings, commit: dict,
                         diffs: list[dict]) -> tuple[dict | None, ScoreOutcome]:
    """Synchronously score one commit and build its rows (the normal path,
    also used by rebase for requests the batch could not deliver)."""
    outcome = score_commit(client, settings, commit, diffs)
    if outcome.result is None:
        return None, outcome
    rows = rows_from_result(
        commit["commit_sha"], outcome.result, diffs,
        outcome.model, settings.prompt_version, outcome.diff_chars,
    )
    if rows is None:
        outcome.failures.append("invalid_json")
    return rows, outcome


# ── Dates ────────────────────────────────────────────────────────────────────

def month_of(value: Any) -> str:
    """``YYYY-MM`` for a date/datetime/ISO string; ``unknown`` when empty."""
    if not value:
        return "unknown"
    return str(value)[:7]


def quarter_of(month: str) -> str:
    """``2026-Q1`` for ``2026-02``; ``unknown`` passes through."""
    m = re.match(r"^(\d{4})-(\d{2})$", month or "")
    if not m:
        return "unknown"
    return f"{m.group(1)}-Q{(int(m.group(2)) - 1) // 3 + 1}"


# ── Failure log ──────────────────────────────────────────────────────────────

class FailureLog:
    """Per-month tally of scoring attempts, for the end-of-run report."""

    def __init__(self) -> None:
        self.scored: dict[str, int] = {}
        self.unscored: dict[str, int] = {}  # commits that failed all attempts
        self.reasons: dict[str, dict[str, int]] = {}  # month -> reason -> failed attempts
        self.dropped: dict[str, int] = {}  # month -> malformed file entries dropped

    def record(self, month: str, ok: bool, failures: Iterable[str],
               dropped: int = 0) -> None:
        bucket = self.scored if ok else self.unscored
        bucket[month] = bucket.get(month, 0) + 1
        if dropped:
            self.dropped[month] = self.dropped.get(month, 0) + dropped
        for reason in failures:
            by_reason = self.reasons.setdefault(month, {})
            by_reason[reason] = by_reason.get(reason, 0) + 1

    def report(self) -> str:
        months = sorted(set(self.scored) | set(self.unscored) | set(self.reasons))
        lines = ["Scoring failures by month:"]
        any_failure = False
        for month in months:
            if not self.unscored.get(month) and not self.reasons.get(month):
                continue
            any_failure = True
            reasons = ", ".join(
                f"{r}={n}" for r, n in sorted(self.reasons.get(month, {}).items())
            ) or "none"
            lines.append(
                f"  {month}: {self.unscored.get(month, 0)} commit(s) unscored, "
                f"{self.scored.get(month, 0)} scored; failed attempts: {reasons}"
            )
        if self.dropped:
            lines.append("  malformed file entries dropped (rest of the commit kept): "
                         + ", ".join(f"{m}={n}" for m, n in sorted(self.dropped.items())))
            any_failure = True
        if not any_failure:
            return "Scoring failures by month: none."
        return "\n".join(lines)


# ── Model helpers ────────────────────────────────────────────────────────────

# (input, output) USD per million tokens at standard rates, for prompts of up
# to 100K tokens (scoring prompts are far below that). The Batch API is 50%.
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
}
BATCH_DISCOUNT = 0.5


def price_for(model: str | None) -> tuple[float, float] | None:
    """Per-million-token price for *model* (dated snapshots resolve by prefix)."""
    if not model:
        return None
    best = None
    for key in PRICING:
        if (model == key or model.startswith(key + "-")) and (best is None or len(key) > len(best)):
            best = key
    return PRICING[best] if best else None


def estimate_cost(model: str | None, input_tokens: float, output_tokens: float,
                  batch: bool = False) -> float | None:
    """USD cost, or None when the model has no entry in ``PRICING``."""
    price = price_for(model)
    if price is None:
        return None
    cost = (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
    return cost * BATCH_DISCOUNT if batch else cost


_HAIKU_NEW = re.compile(r"^claude-haiku-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$")
_HAIKU_OLD = re.compile(r"^claude-(\d+)(?:-(\d{1,2}))?-haiku(?:-\d{8})?$")


def haiku_version(model_id: str) -> tuple[int, int] | None:
    """``(major, minor)`` for Haiku model IDs, else None."""
    for pattern in (_HAIKU_NEW, _HAIKU_OLD):
        m = pattern.match((model_id or "").lower())
        if m:
            return int(m.group(1)), int(m.group(2) or 0)
    return None


def newer_haiku_models(configured: str, available: Iterable[str]) -> list[str]:
    """Haiku IDs in *available* that are newer than *configured*."""
    current = haiku_version(configured)
    if current is None:
        return []
    newer = {
        mid for mid in available
        if (v := haiku_version(mid)) is not None and v > current
    }
    return sorted(newer)


def warn_if_newer_haiku(client: Any, configured: str) -> list[str]:
    """Print a WARNING when a newer Haiku exists than *configured*.

    Never switches models: a switch changes the scale of the series and needs
    ``calibrate`` and ``rebase``.
    """
    try:
        available = [m.id for m in client.models.list(limit=100)]
    except Exception as exc:  # noqa: BLE001
        print(f"Note: could not list models to check for newer Haiku ({exc}).", flush=True)
        return []
    newer = newer_haiku_models(configured, available)
    if newer:
        print(
            f"WARNING: a newer Haiku is available ({', '.join(newer)}) than "
            f"SCORING_MODEL={configured}. Scoring continues with {configured}. "
            "Switching models changes the scale of the scores: run "
            "/coding-productivity:calibrate to compare, then "
            "/coding-productivity:rebase if it says REBASE REQUIRED.",
            flush=True,
        )
    return newer


# ── Consent ──────────────────────────────────────────────────────────────────

def ensure_consent() -> None:
    """Display a one-time notice about sending diffs to the Anthropic API."""
    if CONSENT_FILE.is_file():
        return

    print()
    print("=" * 70)
    print("DATA CLASSIFICATION NOTICE")
    print("=" * 70)
    print(
        "Commit scoring sends file diffs and commit metadata to the\n"
        "Anthropic API (Claude) for evaluation. The data is processed\n"
        "according to Anthropic's API Terms of Service and is NOT used for\n"
        "model training.\n"
        "\n"
        "What is sent:\n"
        "  - File paths and diff content (truncated to ~10 KB per file)\n"
        "  - Commit titles and messages\n"
        "  - Line-change counts\n"
        "\n"
        "What is NOT sent:\n"
        "  - API tokens, credentials, or secrets\n"
        "  - Full file contents (only diff hunks)\n"
        "  - Developer names or emails\n"
    )
    print("=" * 70)
    print()

    CONSENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONSENT_FILE.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
