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
import os
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

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

# Stop reasons that mean "no usable answer", even when the call succeeded.
FAILED_STOP_REASONS = ("refusal", "max_tokens")

FALLBACK_SYSTEM_PROMPT = (
    "You are an expert panel of 3 senior software engineers evaluating code "
    "commits for a productivity analysis. Score each file from 0.0 to 1.0 "
    "based on intellectual effort and value, NOT just lines of code."
)

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
    """Read the scoring rubric shipped in references/."""
    prompt_path = _SCRIPT_DIR.parent / "references" / "commit_scoring_prompt.md"
    if not prompt_path.is_file():
        print(
            f"Warning: scoring prompt not found at {prompt_path}; "
            "using built-in fallback.",
            flush=True,
        )
        return FALLBACK_SYSTEM_PROMPT
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
        message=(commit.get("message") or commit.get("title", "") or "")[:500],
        file_list="\n".join(file_list_lines),
        diffs="\n".join(parts),
    )


# ── Instrument definition ────────────────────────────────────────────────────

def supports_adaptive_thinking(model: str) -> bool:
    """False for Haiku 4.x and the 3.x line: they take neither adaptive
    thinking nor ``output_config.effort``."""
    m = (model or "").lower()
    return not (m.startswith("claude-haiku-4") or m.startswith("claude-3"))


@dataclass(frozen=True)
class ScoringSettings:
    """Everything that defines the measuring instrument."""

    model: str
    system_prompt: str
    effort: str = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS

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
            self.thinking, self.output_config, self.max_tokens,
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


def compute_prompt_version(system_prompt: str, user_template: str, model: str,
                           thinking: Optional[dict], output_config: Optional[dict],
                           max_tokens: int) -> str:
    """First 12 hex of sha256 over everything that defines the instrument."""
    payload = json.dumps(
        {
            "system": system_prompt,
            "user_template": user_template,
            "model": model,
            "thinking": thinking,
            "effort": (output_config or {}).get("effort"),
            "max_tokens": max_tokens,
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


def evaluate_response(response: Any) -> tuple[dict | None, str | None]:
    """Turn a Messages response into ``(score_result, failure_reason)``.

    A refusal or a ``max_tokens`` stop is a failed attempt even though the
    call itself succeeded: the text, if any, is incomplete or absent.
    """
    stop = _get(response, "stop_reason")
    if stop in FAILED_STOP_REASONS:
        return None, stop
    text = extract_text(response)
    if text is None:
        return None, "no_text"
    result = parse_json_response(text)
    if isinstance(result, dict) and "files" in result:
        return result, None
    return None, "invalid_json"


@dataclass
class ScoreOutcome:
    """Result of scoring one commit, including what it cost and what failed."""

    result: dict | None = None
    model: str | None = None  # response.model of the successful call
    input_tokens: int = 0
    output_tokens: int = 0
    failures: list[str] = field(default_factory=list)  # one reason per failed attempt


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
            result, reason = evaluate_response(response)
            if result is not None:
                outcome.result = result
                outcome.model = _get(response, "model")
                return outcome
            outcome.failures.append(reason or "invalid_json")

        if attempt < max_retries - 1:
            sleep(0.5)

    return outcome


# ── Productivity and rows ────────────────────────────────────────────────────

def calculate_productivity(score_result: dict, diffs: list[dict]) -> dict | None:
    """Aggregate per-file scores into commit-level productivity metrics."""
    if not score_result or "files" not in score_result:
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
        path = fs.get("path")
        if path not in diff_lines:
            continue
        try:
            raw_score = float(fs["score"])
        except (KeyError, TypeError, ValueError):
            continue

        info = diff_lines[path]
        lines = info["total"]
        score = apply_score_caps(path, raw_score, lines)

        # productivity = score * max(additions, 1)
        total_productivity += score * max(info["additions"], 1)
        total_lines += lines

        cat = fs.get("category", "unknown")
        if cat not in VALID_CATEGORIES:
            cat = "chore"

        file_scores.append({
            "commit_sha": "",  # filled in by caller
            "file_path": path,
            "score": round(score, 4),
            "category": cat,
            "reasoning": (fs.get("reasoning") or "")[:1000],
            "scored_at": "",  # filled in by caller
        })

    weighted_score = total_productivity / total_lines if total_lines > 0 else 0.0

    return {
        "files": file_scores,
        "total_productivity": round(total_productivity, 4),
        "weighted_score": round(weighted_score, 4),
        "total_lines": total_lines,
        "overall_category": score_result.get("overall_category", "unknown"),
    }


def rows_from_result(sha: str, score_result: dict, diffs: list[dict],
                     scorer_model: str | None, prompt_version: str) -> dict | None:
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
        outcome.model, settings.prompt_version,
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

    def record(self, month: str, ok: bool, failures: Iterable[str]) -> None:
        bucket = self.scored if ok else self.unscored
        bucket[month] = bucket.get(month, 0) + 1
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
