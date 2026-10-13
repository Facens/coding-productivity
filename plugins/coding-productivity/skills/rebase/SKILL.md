---
name: coding-productivity:rebase
description: 'Rescore ALL commits with the configured SCORING_MODEL into a new versioned scoring series using the Message Batches API, then switch the active series. Use after calibrate says REBASE REQUIRED, or whenever the scoring model or prompt changes. Old tables stay untouched.'
argument-hint: '[--no-activate] [--restart]'
---

# Rebase the Scoring Series

Switching the scoring model means rescoring everything, never splicing two models into one series. This skill rescores all commits with the model in `SCORING_MODEL` into new tables `commit_scores_v{N}` and `file_scores_v{N}` (N is the next free version), using the Message Batches API at 50% of standard prices. The previous tables are never modified. The active series switches only after the rebase completes and the per-month scored counts are reported. `/coding-productivity:analyze` and `/coding-productivity:score` then read and write the new series.

Batches can take up to 24 hours. The script is a resumable state machine: each run advances as far as it can and exits, and state is kept in `.coding-productivity/rebase_state.json`.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps.

## Locate the Project Root

The project root is the directory containing `.coding-productivity.env`. All paths below are relative to this root. If a `.coding-productivity/.venv` is missing, run `python3 scripts/setup_env.py` first.

## Step 1: Check Prerequisites

Read `.coding-productivity.env`. Require `SCORING_ENABLED=true`, `ANTHROPIC_API_KEY` and `SCORING_MODEL`. If any is missing, tell the user to run `/coding-productivity:setup` and stop.

If `.coding-productivity/rebase_state.json` exists and its `status` is not `activated`, a rebase is already in progress: skip to Step 4 to resume it.

## Step 2: Confirm the Model

Show the user the model that will score everything (`SCORING_MODEL`, `SCORING_EFFORT`, `SCORING_MAX_TOKENS`) and the active series (see the check in the calibrate skill). If they want a different model, ask them to confirm editing `SCORING_MODEL` in `.coding-productivity.env`; that edit is the intended way to start a new series, and the rebase is what makes it safe. Recommend running `/coding-productivity:calibrate` first if they have not.

## Step 3: Cost and Consent

If a calibration JSON for this model exists in `.coding-productivity/`, read its `rebase_estimate` (`batch_usd`, `eligible_commits`) and show it. Otherwise say the cost is unknown until measured, and suggest calibrating first (a rebase of N commits costs roughly N times the per-commit tokens of a sample, at 50% of standard prices).

Show the data classification notice from the score skill (Step 3) if `.coding-productivity/.scoring_consent` does not exist.

Ask via `AskUserQuestion`:

> This will rescore all {N} commits with {model} through the Batch API{, estimated cost ~${X}}. Existing score tables stay untouched; the active series switches only after the per-month counts are reported and every commit is scored. Proceed?
>
> 1. Yes, start the rebase
> 2. Yes, but build the new series without activating it (`--no-activate`)
> 3. No, cancel

## Step 4: Run (and Resume)

```
cd <project_root> && python3.14 scripts/rebase.py --config .coding-productivity.env [--no-activate] [--wait 540]
```

`--wait 540` keeps polling for up to 9 minutes before exiting, which fits a single tool call. Exit codes:

- **0**: finished. The report and the activation message were printed.
- **3**: batches are still processing. Tell the user it can take up to 24 hours and offer to re-run this step later (for example with `/loop` or by asking again). Do not describe it as monitored unless a re-check is actually scheduled.
- **4**: finished building but not activated: either some commits are still unscored (the report lists reasons), or `--no-activate` was used.
- **1**: error. Display it. If it says the API refused the batch for billing or credit reasons, nothing was marked failed: tell the user to fix the account and re-run to resume. If it says the config no longer matches the unfinished rebase, restore the config, or pass `--restart` to abandon that rebase (its tables stay in storage).

Failed, errored, expired, refused and truncated batch results are retried automatically through the synchronous scorer.

## Step 5: Review the Report

Show the per-month table (eligible commits, previous series, new series, not scored). If commits remain unscored, ask whether to re-run to retry them, or to accept the gap:

```
cd <project_root> && python3.14 scripts/rebase.py --config .coding-productivity.env --activate-version <N> --force
```

Only use `--force` after the user explicitly accepts the gap.

To activate a series built with `--no-activate`:

```
cd <project_root> && python3.14 scripts/rebase.py --config .coding-productivity.env --activate-version <N>
```

## Step 6: Wrap Up

Tell the user the active series version, model and prompt version, and that old tables (`commit_scores`, `file_scores`, or earlier `_v{N}` tables) are untouched and can be dropped later if no longer needed. Suggest `/coding-productivity:analyze`.
