---
name: coding-productivity:calibrate
description: 'Compare a candidate scoring model with the current one before switching. Rescores a deterministic sample of already-scored commits with both, reports ratios and correlations per quarter, and says COMPARABLE or REBASE REQUIRED. Use when a newer Claude model is available or before changing SCORING_MODEL. Writes nothing to the score tables.'
argument-hint: '--candidate MODEL [--sample N]'
---

# Calibrate a Candidate Scoring Model

The scoring model is a measuring instrument. A newer model can change the scale of the scores (for example, Haiku 5.5 scored 0.74x Haiku 4.5 on weighted score in one real dataset, and the ratio differed by period), so a series that mixes two models inflates or deflates trends, and no single conversion factor fixes it.

This skill measures that risk. It draws a deterministic sample of already-scored commits, stratified across months, and scores each commit again with the current model (`SCORING_MODEL`) and with the candidate. The current model's agreement with its own stored scores is the noise floor. It writes nothing to the score tables; results go to a JSON file.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps.

## Locate the Project Root

The project root is the directory containing `.coding-productivity.env`. All paths below are relative to this root. If a `.coding-productivity/.venv` is missing, run `python3 scripts/setup_env.py` first.

## Step 1: Check Prerequisites

Read `.coding-productivity.env`. Require `SCORING_ENABLED=true`, `ANTHROPIC_API_KEY` and `SCORING_MODEL`. If any is missing, tell the user to run `/coding-productivity:setup` and stop.

Check that scores exist:

```
python3.14 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.config import Config
from lib.storage import get_storage
from lib.scoring_meta import get_active
cfg = Config()
st = get_storage(cfg)
a = get_active(st)
print(f'SERIES=v{a.version} MODEL={a.scorer_model} SCORED={st.count(a.commit_table)}')
st.close()
"
```

If `SCORED` is 0, tell the user to run `/coding-productivity:score` first and stop.

## Step 2: Choose the Candidate and Sample Size

If the user did not pass `--candidate`, ask which model to evaluate. Suggest the newest Haiku if the score skill printed a "newer Haiku" warning.

Default `--sample` is 300 commits. Explain the cost: the skill makes about 2 calls per sampled commit (current model and candidate), plus one more per commit with `--fresh-retest`. Each period needs at least 10 commits to be judged, so use a larger sample for long histories.

## Step 3: Legacy Scores

Scores written by earlier plugin versions have no recorded model. The noise floor compares the current model's rescoring with the stored scores, which is only valid if the current model produced them.

- If the sample would contain scores with no recorded model, ask the user which model produced them. If it equals `SCORING_MODEL`, pass `--legacy-model <that model>`.
- If it differs, or the user does not know, pass `--fresh-retest`: the current model scores the sample twice and the stored scores are not used.

(The script stops with an explanatory error if this is needed and missing.)

## Step 4: Confirm and Run

Ask via `AskUserQuestion`:

> This will send about {2 x N} scoring requests to the Anthropic API (diffs are truncated to ~10 KB per file) at standard prices. Proceed?
>
> 1. Yes, run calibration
> 2. No, cancel

Then run:

```
cd <project_root> && python3.14 scripts/calibrate.py --config .coding-productivity.env --candidate <model> --sample <N> [--legacy-model <model>] [--fresh-retest]
```

## Step 5: Interpret the Report

Display the report. It contains, overall and per quarter:

- Mean weighted_score and total_productivity ratio (candidate or retest divided by reference)
- Pearson and Spearman correlation, and the median absolute difference of weighted_score
- The test-retest table (the current model against itself) next to the candidate table
- Token counts and a cost estimate for a full rebase (Batch API at 50% of standard prices)

The verdict is one of:

- **COMPARABLE**: every per-period productivity ratio is within 2x the largest test-retest deviation (floor 0.02) of 1.0, and the spread of the per-period ratios is inside the same band. The candidate behaves like the current model re-run. Switching is still a change of instrument, so say so, but the series can reasonably continue.
- **REBASE REQUIRED**: otherwise. Do not edit `SCORING_MODEL` and keep scoring: that splices two scales into one series. Run `/coding-productivity:rebase`.

Report the JSON path (printed by the script) and the `VERDICT=` line. Never describe a ratio as a conversion factor to apply to old data.

## Step 6: Offer Next Step

Ask via `AskUserQuestion`:

> What next?
>
> 1. Rebase the whole history onto the candidate (`/coding-productivity:rebase`)
> 2. Keep the current model for now
> 3. Calibrate another candidate

If option 1: tell the user to set `SCORING_MODEL` to the candidate in `.coding-productivity.env` when the rebase skill asks, then run `/coding-productivity:rebase`.
