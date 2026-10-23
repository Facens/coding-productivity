---
name: coding-productivity:analyze
description: 'Run interactive productivity analysis on extracted data. Use after extraction to view monthly trends, before/after comparisons, per-author metrics, merge velocity, and category breakdowns.'
argument-hint: '[query type: trends, comparison, authors, velocity, categories, efficiency]'
---

# Analyze Productivity Data

This skill runs interactive analysis queries against the local dataset and presents results as formatted tables.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps.

## Locate the Project Root

The project root is the directory containing `.coding-productivity.env`. All paths below are relative to this root.

## Step 1: Check Data Exists

Run via Bash:
```
python3.14 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.config import Config
from lib.storage import get_storage
from lib.scoring_meta import get_active
cfg = Config()
st = get_storage(cfg)
commits = st.count('commits')
try:
    scores = st.count(get_active(st).commit_table)
except:
    scores = 0
print(f'COMMITS:{commits}')
print(f'SCORES:{scores}')
st.close()
"
```

- If `COMMITS:0`, tell the user: "No commit data found. Run `/coding-productivity:extract` first to pull data." and stop.
- Parse the counts. If `SCORES:0`, note that AI scoring data is unavailable and only raw metrics will be shown.

Duplicate work (merge, squash, promotion and copied commits, moved code) is excluded from every commit-based metric once `/coding-productivity:dedupe` has run. If `analyze.py` warns that it has never run, or that the commits table changed since the last run, tell the user and offer to run `/coding-productivity:dedupe` first. Pass `--require-dedupe` to make a missing run an error instead of a warning.

## Step 2: Ask What Analysis to Run

Ask the user:
> What analysis would you like to run?
>
> 1. Monthly Trends -- commit count, productivity, avg score, developers by month
> 2. Period Comparison -- before/after metrics with % change (e.g. pre/post AI adoption)
> 3. Author Productivity -- per-author: commits, total productivity, avg score, top category
> 4. Merge Velocity -- monthly: MRs, merge rate, avg time to merge, velocity at 7d/30d
> 5. Category Distribution -- commit categories, % of total, avg score
> 6. Code Efficiency -- monthly: total additions vs merged additions, efficiency ratio
> 7. Run all analyses

## Step 3: Collect Date Parameters

For options 1, 3, 4, 5, 6, or 7: ask the user for a date range:
> What date range should the analysis cover?
>
> Enter start and end dates (YYYY-MM-DD), e.g. `2025-01-01 to 2025-12-31`

For option 2 (Period Comparison): ask for two date ranges:
> Enter the **baseline** period (the "before" window):
> Start date (YYYY-MM-DD):
> End date (YYYY-MM-DD):
>
> Enter the **comparison** period (the "after" window):
> Start date (YYYY-MM-DD):
> End date (YYYY-MM-DD):

## Step 4: Run the Analysis

Build and run the appropriate command via Bash. Use `python3.14` to execute the script.

For standard queries (monthly_trends, author_productivity, merge_velocity, category_distribution, code_efficiency):
```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query <query_name> --since <start> --until <end> --format table
```

For period_comparison:
```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query period_comparison \
    --baseline-start <b_start> --baseline-end <b_end> \
    --comparison-start <c_start> --comparison-end <c_end> --format table
```

For "all":
```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query all --since <start> --until <end> --format table
```

## Step 5: Display Results

Present the script output directly. It is already formatted as markdown tables. Keep the `excluded: N commits (...), moved-code discount X% of productivity` line that precedes them: it says how much duplicate work was removed from the selected range.

Keep the `identity:` lines too. They list the identities `analyze` joined into one person (stored names and emails, with commit counts) and the non-person accounts (such as `root`) it left out, so a reader can review them. With anonymization on the lines show pseudonyms, not names: to check a merge, use `/coding-productivity:developers`, which resolves pseudonyms locally. To undo one, add its two emails to `IDENTITY_NO_MERGE` in `.coding-productivity.env` (`email1:email2`), or set `IDENTITY_AUTO_MERGE=false`; `/coding-productivity:developers` records the pair for the user. Developer counts (`unique_authors`) count people, not emails.

Developers who commit once a month still count as developers. When the user wants to see the effect, set `MIN_ACTIVE_COMMITS=N` in `.coding-productivity.env`: monthly trends then gain `active_developers` (people with at least N commits in the month) and `productivity_per_active_developer` (their productivity divided by their number). The default, 1, adds nothing.

If the script exits with a message that the date range mixes scoring models (or mixes legacy scores with a recorded model), do not work around it. Explain that a trend across two models is a trend in the instrument, and offer:

> 1. Narrow the date range to one model
> 2. Rescore everything with one model: `/coding-productivity:calibrate`, then `/coding-productivity:rebase`
> 3. If the legacy scores (no recorded model) are known to come from the same model as the rest, re-run with `--allow-legacy-null`

`--allow-legacy-null` only relaxes NULL-plus-one-model; more than one recorded model, or more than one `prompt_version`, is always refused.

If no scoring data exists and the user ran a score-dependent query (category_distribution), explain:
> Category distribution requires AI scoring data. Run `/coding-productivity:score` first to generate commit scores.

## Step 6: Offer Follow-up

Ask the user:
> Would you like to:
>
> 1. Run another analysis
> 2. Generate a full executive report (`/coding-productivity:report`)
> 3. Done

If option 1, return to Step 2. If option 2, tell the user to run `/coding-productivity:report`.
