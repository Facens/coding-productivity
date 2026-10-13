#!/usr/bin/env python3
"""
Rescore ALL commits with the configured model into a new scoring series.

The scoring model is a measuring instrument: one series = one model. Changing
the model means rescoring everything (a rebase), never splicing two models into
one series. This script:

1. Allocates the next free version N and creates ``commit_scores_v{N}`` /
   ``file_scores_v{N}``. The existing tables are never modified.
2. Submits every eligible commit through the Message Batches API (50% of
   standard prices), chunked below the 100,000 request / 256 MB batch limits.
3. Collects results (they arrive in any order; keyed by ``custom_id``) and
   writes them to the new tables. Failed, errored, expired, canceled, refused
   and truncated results are retried through the normal synchronous scorer.
4. Reports per-month scored counts, old series vs new series.
5. Only then switches the active series (a row in ``scoring_meta``), and only
   if every eligible commit is scored.

Batches take up to 24 hours, so the script is a resumable state machine: each
run advances as far as it can and exits. Re-run it to continue. State lives in
``.coding-productivity/rebase_state.json`` (which also persists the
``custom_id`` -> commit index map: custom_ids are limited to 64 characters).

Exit codes: 0 done, 1 error, 3 batches still processing (run again later),
4 finished but not activated (commits left unscored, or --no-activate).

Usage::

    python rebase.py --config .coding-productivity.env
    python rebase.py --config .coding-productivity.env --wait 540    # poll up to 9 min
    python rebase.py --config .coding-productivity.env --no-activate
    python rebase.py --config .coding-productivity.env --activate-version 2 [--force]
    python rebase.py --config .coding-productivity.env --restart

"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from lib import scoring, scoring_meta
from lib.config import Config
from lib.storage import get_storage

# Message Batches limits: 100,000 requests and 256 MB per batch. Stay well
# below the byte limit: our size is an estimate of the request bodies.
MAX_BATCH_REQUESTS = 100_000
MAX_BATCH_BYTES = 200 * 1024 * 1024
WRITE_EVERY = 500
SYNC_FLUSH_EVERY = 25  # commits between writes (and state saves) in the sync fallback
MAX_CONSECUTIVE_REJECTIONS = 10

EXIT_DONE, EXIT_ERROR, EXIT_PENDING, EXIT_NOT_ACTIVATED = 0, 1, 3, 4


# ---------------------------------------------------------------------------
# Pure helpers (tested without the SDK or a database)
# ---------------------------------------------------------------------------

_BILLING_MARKERS = ("credit balance", "billing", "purchase credits", "insufficient credit",
                    "spend limit", "usage limit", "quota")


def is_billing_error(exc: BaseException) -> bool:
    """True if an API error says the account, not the request, is the problem."""
    text = str(exc).lower()
    return any(marker in text for marker in _BILLING_MARKERS)


def custom_id_for(index: int) -> str:
    """``custom_id`` for the commit at *index* in the state's commit list.

    Short by construction (the API allows 1-64 chars of ``[A-Za-z0-9_-]``);
    the commit SHA is recovered from the persisted index map.
    """
    return f"r{index}"


def index_of(custom_id: str) -> int:
    if not custom_id.startswith("r") or not custom_id[1:].isdigit():
        raise ValueError(f"Unexpected custom_id: {custom_id!r}")
    return int(custom_id[1:])


def request_size(custom_id: str, params: dict) -> int:
    """Upper-bound size in bytes of one batch request line.

    ASCII-escaped JSON is the larger of the two plausible encodings, so the
    estimate never undercounts.
    """
    return len(json.dumps(
        {"custom_id": custom_id, "params": params},
        separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8"))


def chunk_requests(items: Iterable[tuple[str, dict]],
                   max_bytes: int = MAX_BATCH_BYTES,
                   max_requests: int = MAX_BATCH_REQUESTS,
                   size_fn=request_size) -> Iterator[list[tuple[str, dict]]]:
    """Group ``(custom_id, params)`` pairs into batches under both limits.

    Streams: only the current chunk is held in memory. A single request larger
    than *max_bytes* gets a chunk of its own (the API will reject it, and that
    commit then falls back to the synchronous path).
    """
    chunk: list[tuple[str, dict]] = []
    chunk_bytes = 0
    for cid, params in items:
        size = size_fn(cid, params)
        if chunk and (chunk_bytes + size > max_bytes or len(chunk) >= max_requests):
            yield chunk
            chunk, chunk_bytes = [], 0
        chunk.append((cid, params))
        chunk_bytes += size
    if chunk:
        yield chunk


def classify_item(item: Any, expected_model: str | None = None
                  ) -> tuple[dict | None, str | None, tuple[int, int], str | None]:
    """Read one batch result.

    Returns ``(score_result, response_model, (input_tokens, output_tokens),
    failure_reason)``. Anything but a usable answer has a failure reason:
    ``errored``, ``canceled``, ``expired``, or what ``evaluate_response``
    reports (``refusal``, ``max_tokens``, ``model_mismatch``, ``no_text``,
    ``invalid_json``). A "succeeded" result can still carry a refusal, a
    truncated answer or output that breaks the JSON contract.
    """
    result = item.result
    if result.type != "succeeded":
        return None, None, (0, 0), result.type
    message = result.message
    usage = getattr(message, "usage", None)
    tokens = (
        (getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
        if usage is not None else (0, 0)
    )
    score_result, reason = scoring.evaluate_response(message, expected_model)
    return score_result, getattr(message, "model", None), tokens, reason


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def state_path(config: Config) -> Path:
    return Path(config._path).parent / ".coding-productivity" / "rebase_state.json"


def load_state(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a crash never leaves half a state file


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _eligible_shas(storage, excluded: list[str]) -> list[str]:
    """Commits to rescore: those with diffs, minus excluded developers."""
    rows = storage.query(
        "SELECT c.commit_sha, c.author_email, c.author_name FROM commits c "
        "WHERE c.commit_sha IN (SELECT DISTINCT commit_sha FROM diffs) "
        "ORDER BY c.authored_date, c.commit_sha"
    )
    if excluded:
        low = {e.lower() for e in excluded}
        rows = [
            r for r in rows
            if (r.get("author_email") or "").lower() not in low
            and (r.get("author_name") or "").lower() not in low
        ]
    return [r["commit_sha"] for r in rows]


def _commit(storage, sha: str) -> dict:
    rows = storage.query("SELECT * FROM commits WHERE commit_sha = $sha", {"sha": sha})
    return rows[0] if rows else {"commit_sha": sha}


def _diffs(storage, sha: str) -> list[dict]:
    return storage.query("SELECT * FROM diffs WHERE commit_sha = $sha", {"sha": sha})


def _write_rows(storage, state: dict, commit_rows: list[dict], file_rows: list[dict]) -> None:
    """File rows first, commit rows last: a commit row is the completion
    marker (resume skips commits that have one), so a crash between the two
    inserts leaves the commit unscored and retried, never half-visible."""
    if file_rows:
        storage.insert_batch(state["file_table"], file_rows)
    if commit_rows:
        storage.insert_batch(state["commit_table"], commit_rows)
    commit_rows.clear()
    file_rows.clear()


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def _iter_requests(storage, state: dict, settings: scoring.ScoringSettings,
                   start: int) -> Iterator[tuple[str, dict]]:
    for i in range(start, len(state["shas"])):
        sha = state["shas"][i]
        prompt = scoring.build_user_prompt(_commit(storage, sha), _diffs(storage, sha))
        yield custom_id_for(i), settings.build_params(prompt)


def _submit_chunk(client, state: dict, chunk: list[tuple[str, dict]], save,
                  counters: dict) -> None:
    """Create one batch; if the API rejects it (400), bisect to isolate the
    offending request(s) and send those to the synchronous path."""
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    requests = [
        Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**params))
        for cid, params in chunk
    ]
    first, last = index_of(chunk[0][0]), index_of(chunk[-1][0])
    try:
        batch = client.messages.batches.create(requests=requests)
    except anthropic.APIStatusError as exc:
        # 400 (invalid request) and 413 (too large) can be caused by one
        # request; anything else is not ours to handle.
        if getattr(exc, "status_code", None) not in (400, 413):
            raise
        if is_billing_error(exc):
            # Not a problem with the requests: bisecting would only mark good
            # commits bad. Stop; the state is saved and a re-run resumes.
            raise RuntimeError(
                "The Anthropic API refused the batch for billing or credit reasons "
                f"({exc}). Nothing was marked failed. Fix the account (credits, "
                "limits) and run this command again to resume."
            ) from exc
        if len(chunk) > 1:
            mid = len(chunk) // 2
            _submit_chunk(client, state, chunk[:mid], save, counters)
            _submit_chunk(client, state, chunk[mid:], save, counters)
            return
        counters["rejected_in_a_row"] += 1
        if counters["rejected_in_a_row"] >= MAX_CONSECUTIVE_REJECTIONS:
            raise RuntimeError(
                f"The Batch API rejected {MAX_CONSECUTIVE_REJECTIONS} requests in a row; "
                "the request shape is probably invalid for this model, or the account "
                f"has a billing or quota problem. Last error: {exc}"
            ) from exc
        sha = state["shas"][first]
        state["failed"][sha] = "bad_request"
        state["batches"].append({
            "id": None, "start": first, "end": last + 1, "processed": True,
            "rejected": True, "error": str(exc)[:300],
        })
        save()
        print(f"  request {chunk[0][0]} rejected by the Batch API; it will be scored "
              "synchronously", flush=True)
        return

    counters["rejected_in_a_row"] = 0
    state["batches"].append({
        "id": batch.id, "start": first, "end": last + 1, "processed": False,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    })
    save()  # persist right away: a lost batch id is paid-for work
    print(f"  batch {batch.id}: {len(chunk):,} requests", flush=True)


def submit_remaining(client, storage, state: dict, settings: scoring.ScoringSettings,
                     save, max_bytes: int, max_requests: int) -> None:
    start = state["batches"][-1]["end"] if state["batches"] else 0
    total = len(state["shas"])
    if start >= total:
        return
    print(f"Submitting {total - start:,} of {total:,} commits as batches...", flush=True)

    counters = {"rejected_in_a_row": 0}
    for chunk in chunk_requests(_iter_requests(storage, state, settings, start),
                                max_bytes, max_requests):
        _submit_chunk(client, state, chunk, save, counters)


def process_batch(client, storage, state: dict, entry: dict, settings, save) -> None:
    """Write one ended batch's results; queue failures for the sync path.

    One malformed result never aborts the rest: it is counted, marked failed
    (retried synchronously later) and the loop moves on. The batch is always
    marked processed at the end.
    """
    existing = storage.get_existing_shas(state["commit_table"])  # resume safety
    commit_rows: list[dict] = []
    file_rows: list[dict] = []
    counts: Counter = Counter()

    for item in client.messages.batches.results(entry["id"]):
        try:
            sha = state["shas"][index_of(item.custom_id)]
        except (ValueError, IndexError, AttributeError):
            counts["unknown_custom_id"] += 1
            continue
        if sha in existing:
            continue

        try:
            score_result, model, (tin, tout), reason = classify_item(item, settings.model)
        except Exception:  # noqa: BLE001 - a malformed item must not stop the batch
            score_result, model, tin, tout, reason = None, None, 0, 0, "malformed_result"
        state["tokens"]["batch_input"] += tin
        state["tokens"]["batch_output"] += tout

        rows = None
        if score_result is not None:
            diffs = _diffs(storage, sha)
            try:
                rows = scoring.rows_from_result(
                    sha, score_result, diffs, model, settings.prompt_version,
                    scoring.DIFF_SIZES[0],  # batch requests always use the first level
                )
            except Exception:  # noqa: BLE001
                rows = None
            if rows is None:
                reason = "invalid_json"
        if rows is None:
            counts[reason or "invalid_json"] += 1
            state["failed"][sha] = reason or "invalid_json"
            continue

        counts["succeeded"] += 1
        if score_result.get("dropped_files"):
            counts["dropped_file_entries"] += score_result["dropped_files"]
        commit_rows.append(rows["commit_row"])
        file_rows.extend(rows["file_rows"])
        if len(commit_rows) >= WRITE_EVERY:
            _write_rows(storage, state, commit_rows, file_rows)

    _write_rows(storage, state, commit_rows, file_rows)
    entry["processed"] = True
    entry["counts"] = dict(counts)
    save()
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no new results"
    print(f"  batch {entry['id']} processed: {summary}", flush=True)


def collect(client, storage, state: dict, settings, save) -> bool:
    """Process every ended batch. Returns True while some are still running."""
    pending = False
    for entry in state["batches"]:
        if entry["processed"]:
            continue
        batch = client.messages.batches.retrieve(entry["id"])
        if batch.processing_status != "ended":
            counts = batch.request_counts
            print(f"  batch {entry['id']}: {batch.processing_status} "
                  f"(processing={counts.processing}, succeeded={counts.succeeded}, "
                  f"errored={counts.errored})", flush=True)
            pending = True
            continue
        process_batch(client, storage, state, entry, settings, save)
    return pending


def sync_fallback(client, storage, state: dict, settings, workers: int, save,
                  flush_every: int = SYNC_FLUSH_EVERY) -> None:
    """Retry failed commits through the normal synchronous scorer.

    Results are written and the state saved every *flush_every* commits (and on
    the way out of any error or interrupt), so a killed run keeps what it paid
    for and the next run converges. All storage access stays on this thread.
    """
    existing = storage.get_existing_shas(state["commit_table"])
    for sha in [s for s in state["failed"] if s in existing]:
        del state["failed"][sha]  # written by an earlier run that died before saving
    todo = list(state["failed"])
    if not todo:
        return
    print(f"Retrying {len(todo):,} commit(s) the batch could not score, synchronously...",
          flush=True)

    # DuckDB connections are not thread-safe: read on the main thread.
    inputs = {sha: (_commit(storage, sha), _diffs(storage, sha)) for sha in todo}

    commit_rows: list[dict] = []
    file_rows: list[dict] = []
    recovered = 0

    def flush() -> None:
        _write_rows(storage, state, commit_rows, file_rows)
        save()

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(scoring.score_and_build_rows, client, settings, commit, diffs): sha
            for sha, (commit, diffs) in inputs.items()
        }
        for fut in as_completed(futures):
            sha = futures[fut]
            try:
                rows, outcome = fut.result()
            except Exception:  # noqa: BLE001 - one bad commit must not stop the rest
                state["failed"][sha] = "error"
                continue
            state["tokens"]["sync_input"] += outcome.input_tokens
            state["tokens"]["sync_output"] += outcome.output_tokens
            if rows is None:
                state["failed"][sha] = ",".join(outcome.failures) or "no_diffs"
                continue
            del state["failed"][sha]
            recovered += 1
            commit_rows.append(rows["commit_row"])
            file_rows.extend(rows["file_rows"])
            if len(commit_rows) >= flush_every:
                flush()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        flush()
    print(f"  {recovered:,} recovered, {len(todo) - recovered:,} still unscored.", flush=True)


# ---------------------------------------------------------------------------
# Report and activation
# ---------------------------------------------------------------------------

def monthly_counts(storage, state: dict, old_commit_table: str) -> tuple[list[tuple], int]:
    """Per-month ``(month, eligible, old, new, missing)`` and total missing."""
    eligible = set(state["shas"])
    months = {
        r["commit_sha"]: scoring.month_of(r["authored_date"])
        for r in storage.query("SELECT commit_sha, authored_date FROM commits")
        if r["commit_sha"] in eligible
    }
    old = storage.get_existing_shas(old_commit_table) & eligible
    new = storage.get_existing_shas(state["commit_table"]) & eligible

    elig_c, old_c, new_c = Counter(), Counter(), Counter()
    for sha in eligible:
        elig_c[months.get(sha, "unknown")] += 1
    for sha in old:
        old_c[months.get(sha, "unknown")] += 1
    for sha in new:
        new_c[months.get(sha, "unknown")] += 1

    rows = [
        (m, elig_c[m], old_c[m], new_c[m], elig_c[m] - new_c[m])
        for m in sorted(elig_c)
    ]
    return rows, len(eligible) - len(new)


def print_report(storage, state: dict, old_commit_table: str) -> int:
    rows, missing = monthly_counts(storage, state, old_commit_table)
    print()
    print(f"Scored commits per month, new series v{state['version']} "
          f"({state['model']}, prompt_version {state['prompt_version']}) vs previous:")
    print("| month | eligible | previous series | new series | not scored |")
    print("|---|---|---|---|---|")
    for m, e, o, n, miss in rows:
        print(f"| {m} | {e:,} | {o:,} | {n:,} | {miss:,} |")
    tot = [sum(r[i] for r in rows) for i in range(1, 5)]
    print(f"| total | {tot[0]:,} | {tot[1]:,} | {tot[2]:,} | {tot[3]:,} |")

    t = state["tokens"]
    batch_cost = scoring.estimate_cost(state["model"], t["batch_input"], t["batch_output"], batch=True)
    sync_cost = scoring.estimate_cost(state["model"], t["sync_input"], t["sync_output"])
    if batch_cost is None:
        cost = "no price known for this model"
    else:
        cost = f"~${batch_cost + (sync_cost or 0):,.2f}"
    print(f"\nTokens: batch {t['batch_input']:,} in / {t['batch_output']:,} out, "
          f"synchronous {t['sync_input']:,} in / {t['sync_output']:,} out ({cost}).")
    if state["failed"]:
        reasons = Counter(state["failed"].values())
        print("Unscored commits by reason: "
              + ", ".join(f"{k}={v}" for k, v in reasons.most_common()))
    return missing


def activate(storage, state: dict) -> None:
    scoring_meta.record(storage, state["version"], "active", state["model"],
                        state["prompt_version"])
    state["status"] = "activated"
    print(f"\nActivated scoring series v{state['version']}: analyze and score now use "
          f"{state['commit_table']} / {state['file_table']}. "
          f"The previous tables are untouched and can be dropped when no longer needed.",
          flush=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _make_client(api_key: str):
    try:
        import anthropic
    except ImportError:
        print("Error: the 'anthropic' package is required.\n"
              "Install it with:  pip install anthropic", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    return anthropic.Anthropic(api_key=api_key)


def _new_state(storage, settings: scoring.ScoringSettings, config: Config) -> dict:
    shas = _eligible_shas(storage, config.EXCLUDED_DEVELOPERS)
    if not shas:
        print("Error: no commits with diffs to score. Run /coding-productivity:extract first.",
              file=sys.stderr)
        sys.exit(EXIT_ERROR)
    version = scoring_meta.next_free_version(storage)
    commit_table, file_table = scoring_meta.table_names(version)
    storage.ensure_table(commit_table)
    storage.ensure_table(file_table)
    scoring_meta.record(storage, version, "building", settings.model, settings.prompt_version)
    return {
        "state_version": 1,
        "version": version,
        "commit_table": commit_table,
        "file_table": file_table,
        "model": settings.model,
        "prompt_version": settings.prompt_version,
        "effort": settings.effort,
        "max_tokens": settings.max_tokens,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "shas": shas,  # the index map: custom_id r{i} -> shas[i]
        "batches": [],
        "failed": {},
        "tokens": {"batch_input": 0, "batch_output": 0, "sync_input": 0, "sync_output": 0},
    }


def run(config: Config, args: argparse.Namespace) -> int:
    path = state_path(config)
    settings = scoring.settings_or_exit(config)

    storage = get_storage(config)
    storage.create_tables()  # also adds provenance columns to older tables

    # --- activate an already built version -------------------------------
    if args.activate_version is not None:
        state = load_state(path)
        if not state or state["version"] != args.activate_version:
            print(f"Error: no rebase state for version {args.activate_version} "
                  f"at {path}.", file=sys.stderr)
            return EXIT_ERROR
        old = scoring_meta.get_active(storage)
        missing = print_report(storage, state, old.commit_table)
        if missing and not args.force:
            print(f"\n{missing:,} eligible commit(s) are not scored in v{state['version']}. "
                  "Not activating. Re-run without --activate-version to retry them, or add --force.")
            return EXIT_NOT_ACTIVATED
        activate(storage, state)
        save_state(path, state)
        return EXIT_DONE

    client = _make_client(config.ANTHROPIC_API_KEY)
    scoring.ensure_consent()
    scoring.warn_if_newer_haiku(client, settings.model)

    # --- load or create state --------------------------------------------
    state = None if args.restart else load_state(path)
    if state and state.get("status") == "activated":
        state = None  # that rebase is finished; this is a new one
    if state:
        if state["model"] != settings.model or state["prompt_version"] != settings.prompt_version:
            print(f"Error: an unfinished rebase (v{state['version']}) was started with "
                  f"{state['model']} / prompt_version {state['prompt_version']}, but the "
                  f"config now gives {settings.model} / {settings.prompt_version}. "
                  "Restore the config to resume, or pass --restart to abandon that rebase "
                  "(its tables stay in storage).", file=sys.stderr)
            return EXIT_ERROR
        print(f"Resuming rebase v{state['version']} ({state['model']}): "
              f"{len(state['shas']):,} commits, {len(state['batches'])} batch(es) submitted.")
    else:
        old = scoring_meta.get_active(storage)
        if old.from_meta and old.scorer_model == settings.model \
                and old.prompt_version == settings.prompt_version:
            print(f"Note: the active series v{old.version} already uses {settings.model} "
                  f"with the same prompt_version; a rebase will reproduce it with new noise.")
        state = _new_state(storage, settings, config)
        print(f"Starting rebase into v{state['version']} "
              f"({state['commit_table']} / {state['file_table']}) with {settings.model}, "
              f"prompt_version {settings.prompt_version}: {len(state['shas']):,} commits.")

    def save() -> None:
        save_state(path, state)

    save()

    # --- submit, collect, fall back --------------------------------------
    submit_remaining(client, storage, state, settings, save,
                     args.max_batch_bytes, args.max_batch_requests)

    deadline = time.time() + args.wait
    pending = collect(client, storage, state, settings, save)
    while pending and time.time() < deadline:
        time.sleep(min(args.poll_interval, max(1.0, deadline - time.time())))
        pending = collect(client, storage, state, settings, save)
    if pending:
        print("\nBatches are still processing (they can take up to 24 hours). "
              "Run this command again later to continue.")
        return EXIT_PENDING

    sync_fallback(client, storage, state, settings, args.workers, save)

    # --- report, then (maybe) switch -------------------------------------
    old = scoring_meta.get_active(storage)
    missing = print_report(storage, state, old.commit_table)
    state["status"] = "complete" if missing == 0 else "running"
    save()

    if missing:
        print(f"\n{missing:,} eligible commit(s) are still unscored. Not activating v{state['version']}. "
              "Run again to retry them, or accept the gap with --activate-version "
              f"{state['version']} --force.")
        return EXIT_NOT_ACTIVATED
    if args.no_activate:
        print(f"\nv{state['version']} is complete but NOT active (--no-activate). "
              f"Switch with: --activate-version {state['version']}")
        return EXIT_NOT_ACTIVATED

    activate(storage, state)
    save()
    return EXIT_DONE


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rescore all commits with SCORING_MODEL into a new scoring series (Batch API)."
    )
    parser.add_argument("--config", "-c", default=None,
                        help="Path to .coding-productivity.env (auto-detected if omitted).")
    parser.add_argument("--wait", type=int, default=0,
                        help="Seconds to keep polling for batches before exiting (default: 0 = check once).")
    parser.add_argument("--poll-interval", type=int, default=60,
                        help="Seconds between polls while waiting (default: 60).")
    parser.add_argument("--workers", "-w", type=int, default=10,
                        help="Parallel workers for the synchronous retry path (default: 10).")
    parser.add_argument("--no-activate", action="store_true",
                        help="Build the new series but do not switch the active one.")
    parser.add_argument("--activate-version", type=int, default=None, metavar="N",
                        help="Report and activate an already built series N; no scoring.")
    parser.add_argument("--force", action="store_true",
                        help="With --activate-version: activate even if commits are unscored.")
    parser.add_argument("--restart", action="store_true",
                        help="Abandon an unfinished rebase and start a new one (new version number).")
    parser.add_argument("--max-batch-bytes", type=int, default=MAX_BATCH_BYTES,
                        help="Byte budget per batch (default: 200 MB; API limit 256 MB).")
    parser.add_argument("--max-batch-requests", type=int, default=MAX_BATCH_REQUESTS,
                        help="Request budget per batch (default and API limit: 100,000).")
    args = parser.parse_args()

    config = Config(args.config) if args.config else Config()
    errors = config.validate()
    if errors:
        for e in errors:
            print(f"Config error: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    if not config.SCORING_ENABLED:
        print("Scoring is disabled. Set SCORING_ENABLED=true in your config.", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    sys.exit(run(config, args))


if __name__ == "__main__":
    main()
